"""检索评测夹具(S6-8):对真实库跑手标 query,算 hit@1 / hit@k / MRR / 期望条目平均名次。

这步是"跑数据找感觉"类问题(overview 写法、topk、w_activation)的度量前提。
**对真实库跑(不设 fake 环境变量),不进 verify 回归。**

夹具文件 `eval/queries.jsonl` 每行一个 JSON 对象(# 注释行 / 空行跳过):
    {"query": "...", "mode": "episode|detail|concept", "expect": ["ep_xxxx"],
     "type": "semantic|verbatim|short|concept|multi|negative|temporal", "note": "..."}
  - query   检索词;concept 模式下是 node 的 label 或别名。
  - mode    episode(向量+FTS 双路,排名取 primary 主槽)/ detail(FTS grep,取 hits)/
            concept(膜 join,取 episodes);可选 "context" 字段仅 concept 用。
  - expect  期望命中的 episode public_id 列表;**空数组 = 负例**(库里无相关记忆,测误召回)。
  - type    评测分组维度(哪条检索路);老夹具无 type 视为 "semantic"。
  - note    人读理由,脚本忽略。

评测口径:
  - 每条 query 跑对应 recall,**touch=False**(只读、不刷时钟,evaluate 不污染衰减态),
    且不走重构(拿结构化候选集直接看名次)。
  - 把结果排成一列 public_id(episode=primary 主槽;detail=hits;concept=episodes),
    对每个 expect 求其 1-based 名次;best_rank = 最靠前的命中名次。
  - 正例指标(expect 非空):hit@1 = best_rank==1;hit@k = best_rank<=k
    (k 默认 = RecallConfig.topk_final,--k 覆盖);MRR = mean(1/best_rank),miss 计 0;
    mean_best_rank = 命中 query 的 best_rank 均值。
  - 负例指标(expect 为空,不进正例分母):负例数 / top-k 内误召回条数均值 /
    干净负例占比(top-k 空手即干净)。
  - 总表之外按 mode、type 各出一张分组小结。

用法:
    .venv/bin/python scripts/eval_recall.py                       # 跑 eval/queries.jsonl
    .venv/bin/python scripts/eval_recall.py --file /tmp/q.jsonl   # 指定夹具(临时库测试用)
    .venv/bin/python scripts/eval_recall.py --k 5                 # 改 hit@k 的 k
    .venv/bin/python scripts/eval_recall.py --param w_activation=0.5 --param topk_final=5  # A/B 临时覆盖
    .venv/bin/python scripts/eval_recall.py --verbose             # miss/误召回打出实际 top-k
    .venv/bin/python scripts/eval_recall.py --out report.json     # 完整留档,两份 diff 即 A/B 报告
    .venv/bin/python scripts/eval_recall.py --selftest            # 离线 fake 自测(不碰真实库)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_QUERIES = _REPO / "eval" / "queries.jsonl"


# ---- 夹具读取 ----
def load_queries(path: Path) -> list[dict]:
    """读 jsonl,跳过 # 注释行与空行;基本字段校验;无 type 视为 semantic。"""
    out: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}:{lineno} 不是合法 JSON: {e}")
        if obj.get("mode") not in ("episode", "detail", "concept"):
            raise SystemExit(f"{path}:{lineno} mode 必须是 episode/detail/concept: {obj!r}")
        obj.setdefault("expect", [])
        obj.setdefault("type", "semantic")
        out.append(obj)
    return out


# ---- 参数覆盖(A/B)----
def apply_params(rc, params: list[str]):
    """--param name=value 临时覆盖 RecallConfig 字段(按现值类型转型)。"""
    overrides: dict = {}
    for p in params:
        if "=" not in p:
            raise SystemExit(f"--param 需 name=value 形式: {p!r}")
        key, val = p.split("=", 1)
        key = key.strip()
        if not hasattr(rc, key):
            raise SystemExit(f"RecallConfig 无字段 {key!r}")
        cur = getattr(rc, key)
        if isinstance(cur, bool):
            overrides[key] = val.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, tuple):
            overrides[key] = tuple(float(x) for x in val.split(","))
        elif isinstance(cur, int):
            overrides[key] = int(val)
        elif isinstance(cur, float):
            overrides[key] = float(val)
        else:
            overrides[key] = val
    return replace(rc, **overrides) if overrides else rc


# ---- 单条检索(只读、不重构)----
def ranked_items(result: dict, mode: str) -> list[tuple[str, str]]:
    """把结果排成 (public_id, 展示文本) 一列:episode=primary 的 overview;
    detail=hits 的 snippet(字段名 window);concept=episodes 的 overview(结果里
    只有 summary 级字段时回退 summary)。文本仅供 --verbose,排名只看 id。"""
    if mode == "detail":
        return [(h["public_id"], h.get("window") or "") for h in result["hits"]]
    if mode == "episode":
        return [(p["public_id"], p.get("overview") or "") for p in result["slots"]["primary"]]
    if mode == "concept":
        return [(e["public_id"], e.get("overview") or e.get("summary") or "")
                for e in result["episodes"]]
    raise ValueError(f"未知 mode: {mode}")


def ranked_ids(result: dict, mode: str) -> list[str]:
    return [pid for pid, _ in ranked_items(result, mode)]


def run_query(cfg, q: dict, now: datetime | None) -> dict:
    from memory_system.recall import recall_concept, recall_detail, recall_episode
    from memory_system.recall.concept import NodeMissError

    mode, query = q["mode"], q["query"]
    if mode == "detail":
        return recall_detail(cfg, query, touch=False, now=now)
    if mode == "episode":
        return recall_episode(cfg, query, touch=False, now=now)
    if mode == "concept":
        try:
            return recall_concept(cfg, query, context=q.get("context"), touch=False, now=now)
        except NodeMissError:
            return {"mode": "concept", "node": query, "alias_bridge": None, "episodes": []}
    raise ValueError(f"未知 mode: {mode}")


# ---- 评测 ----
def _print_topk(rec: dict, items: list[tuple[str, str]], reason: str) -> None:
    """--verbose:打出实际返回的 top-k(public_id + 展示文本前 40 字)。"""
    exp = ",".join(rec["expect"]) if rec["expect"] else "(负例)"
    print(f"[verbose] {reason}: {rec['query']!r} "
          f"(mode={rec['mode']} type={rec['type']}) 期望={exp}")
    if not items:
        print("    (top-k 空手)")
    for i, (pid, text) in enumerate(items, start=1):
        t = (text or "").replace("\n", " ")
        if len(t) > 40:
            t = t[:40] + "…"
        print(f"    {i}. {pid}  {t}")


def _stats(rows: list[dict], k: int) -> dict:
    """对一组 per-query 记录算汇总:正例 hit/MRR/名次 + 负例误召回。无正例的指标为 None。"""
    pos = [r for r in rows if not r["negative"]]
    neg = [r for r in rows if r["negative"]]
    found = [r["best_rank"] for r in pos if r["best_rank"] is not None]
    n_pos, n_neg = len(pos), len(neg)
    return {
        "n": len(rows),
        "n_pos": n_pos,
        "n_neg": n_neg,
        "hit1_rate": sum(r["hit1"] for r in pos) / n_pos if n_pos else None,
        "hitk_rate": sum(r["hitk"] for r in pos) / n_pos if n_pos else None,
        "mrr": sum(r["rr"] for r in pos) / n_pos if n_pos else None,
        "mean_best_rank": sum(found) / len(found) if found else None,
        "coverage": f"{len(found)}/{n_pos}",
        "neg_mean_false": sum(r["n_false"] for r in neg) / n_neg if n_neg else None,
        "neg_clean_rate": sum(r["clean"] for r in neg) / n_neg if n_neg else None,
    }


def evaluate(cfg, queries: list[dict], k: int, now: datetime | None = None,
             verbose: bool = False) -> dict:
    per: list[dict] = []
    for q in queries:
        res = run_query(cfg, q, now)
        items = ranked_items(res, q["mode"])
        ids = [pid for pid, _ in items]
        expect = q.get("expect") or []
        negative = not expect  # expect 为空数组 = 负例,不进正例分母
        rec: dict = {
            "query": q["query"], "mode": q["mode"],
            "type": q.get("type") or "semantic",
            "negative": negative, "expect": list(expect),
            "topk_ids": ids[:k],
        }
        if negative:
            n_false = len(ids[:k])  # 负例返回的每一条都是误召回
            rec.update({"n_expect": 0, "n_found": 0, "best_rank": None,
                        "hit1": None, "hitk": None, "rr": None,
                        "n_false": n_false, "clean": n_false == 0})
            if verbose and n_false:
                _print_topk(rec, items[:k], reason="误召回")
        else:
            ranks = [ids.index(e) + 1 if e in ids else None for e in expect]
            found = [r for r in ranks if r is not None]
            best = min(found) if found else None
            rec.update({"n_expect": len(expect), "n_found": len(found),
                        "best_rank": best,
                        "hit1": best == 1,
                        "hitk": best is not None and best <= k,
                        "rr": 1.0 / best if best is not None else 0.0,  # MRR:miss 计 0
                        "n_false": None, "clean": None})
            if verbose and not rec["hitk"]:
                _print_topk(rec, items[:k], reason="miss")
        per.append(rec)

    total = _stats(per, k)
    # 分组(保持夹具出现顺序,报表可复读)
    modes = list(dict.fromkeys(r["mode"] for r in per))
    types = list(dict.fromkeys(r["type"] for r in per))
    return {
        "per_query": per,
        "n": total["n"],
        "k": k,
        "n_pos": total["n_pos"],
        "hit1_rate": total["hit1_rate"],
        "hitk_rate": total["hitk_rate"],
        "mrr": total["mrr"],
        "mean_best_rank": total["mean_best_rank"],
        "coverage": total["coverage"],
        "negatives": {
            "n": total["n_neg"],
            "mean_false_topk": total["neg_mean_false"],
            "clean_rate": total["neg_clean_rate"],
        },
        "by_mode": {m: _stats([r for r in per if r["mode"] == m], k) for m in modes},
        "by_type": {t: _stats([r for r in per if r["type"] == t], k) for t in types},
    }


# ---- 报表打印 ----
def _fmt(v, spec: str = ".3f") -> str:
    return "-" if v is None else format(v, spec)


def _print_group_table(title: str, groups: dict[str, dict], k: int) -> None:
    """分组小结:正例三指标一张表;含负例的组在表下补负例行。"""
    print(f"\n按 {title} 分组:")
    gw = min(24, max(8, *(len(g) for g in groups)))
    header = f"  {title:<{gw}} {'n':>4} {'hit@1':>7} {'hit@'+str(k):>7} {'MRR':>7}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for name, s in groups.items():
        print(f"  {name:<{gw}} {s['n']:>4} {_fmt(s['hit1_rate']):>7} "
              f"{_fmt(s['hitk_rate']):>7} {_fmt(s['mrr']):>7}")
    for name, s in groups.items():
        if s["n_neg"]:
            print(f"  负例@{name}: n={s['n_neg']}  top-{k} 误召回均值="
                  f"{_fmt(s['neg_mean_false'], '.2f')}  干净占比={_fmt(s['neg_clean_rate'])}")


def print_report(report: dict) -> None:
    k = report["k"]
    per = report["per_query"]
    if not per:
        print("(夹具为空:eval/queries.jsonl 尚无 query,先添加手标条目再评测。)")
        return
    qw = min(40, max(12, *(len(r["query"]) for r in per)))
    tw = min(10, max(4, *(len(r["type"]) for r in per)))
    header = (f"{'query':<{qw}}  {'mode':<8} {'type':<{tw}} {'expect':>6} "
              f"{'rank':>5} {'hit@1':>6} {'hit@'+str(k):>6}")
    print(header)
    print("-" * len(header))
    for r in per:
        q = r["query"] if len(r["query"]) <= qw else r["query"][: qw - 1] + "…"
        rank = "-" if r["best_rank"] is None else str(r["best_rank"])
        if r["negative"]:
            h1 = hk = "-"  # 负例不进 hit 指标,主表只占位
        else:
            h1, hk = ("✓" if r["hit1"] else "·"), ("✓" if r["hitk"] else "·")
        print(f"{q:<{qw}}  {r['mode']:<8} {r['type']:<{tw}} {r['n_expect']:>6} "
              f"{rank:>5} {h1:>6} {hk:>6}")
    print("-" * len(header))
    print(f"正例 n={report['n_pos']}  hit@1={_fmt(report['hit1_rate'])}  "
          f"hit@{k}={_fmt(report['hitk_rate'])}  MRR={_fmt(report['mrr'])}  "
          f"平均名次={_fmt(report['mean_best_rank'], '.2f')}  覆盖={report['coverage']}")
    neg = report["negatives"]
    if neg["n"]:
        print(f"负例 n={neg['n']}  top-{k} 误召回均值={_fmt(neg['mean_false_topk'], '.2f')}  "
              f"干净负例占比={_fmt(neg['clean_rate'])}")
    _print_group_table("mode", report["by_mode"], k)
    _print_group_table("type", report["by_type"], k)


# ---- 报告落盘(--out)----
def _manifest_summary(path: Path) -> dict | None:
    """eval/manifest.json 存在则内联其内容摘要:超长的映射/列表折叠成条数,不整段搬运。"""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"manifest 读取失败: {e}"}
    if not isinstance(raw, dict):
        return {"error": "manifest 不是 JSON 对象"}
    out: dict = {}
    for key, val in raw.items():
        if isinstance(val, (dict, list)) and len(val) > 12:
            out[key] = f"({len(val)} 项,略)"
        else:
            out[key] = val
    return out


def dump_report(path: Path, report: dict, rc, param_overrides: list[str],
                manifest_path: Path) -> None:
    """完整留档:RecallConfig 快照(含 --param 覆盖后)+ 每 query 明细 + 各汇总。
    两份 report diff 即 A/B 报告。"""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "k": report["k"],
        "recall_config": asdict(rc),
        "param_overrides": list(param_overrides),
        "summary": {key: report[key] for key in (
            "n", "n_pos", "hit1_rate", "hitk_rate", "mrr",
            "mean_best_rank", "coverage", "negatives")},
        "by_mode": report["by_mode"],
        "by_type": report["by_type"],
        "per_query": report["per_query"],
        "manifest": _manifest_summary(manifest_path),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"[out] 报告已写 {path}")


# ---- 离线 fake 自测(不碰真实库)----
def run_selftest() -> int:
    """临时库 + 已知 query,验证报表数字正确。设 fake 环境变量后再 load_config,全程离线。"""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="memsys_eval_")
    os.environ["MEMORY_SYSTEM_HOME"] = tmp
    os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
    os.environ["MEMORY_EMBED_DIM"] = "16"
    os.environ["MEMORY_AGENT_PROVIDER"] = "fake"

    from memory_system.config import load_config
    from memory_system.embedding.fake import FakeProvider
    from memory_system.fragments import Episode, Node, write_episode, write_node
    from memory_system.index import rebuild

    cfg = load_config()
    for d in cfg.all_dirs():
        d.mkdir(parents=True, exist_ok=True)
    print(f"[selftest] 临时库: {tmp}")

    now = datetime(2026, 7, 2, 0, 0, 0, tzinfo=timezone.utc)
    # 语料(fake 向量:只有**完全相同**文本才距离 0):
    #  ep_self0001 overview 与 episode query 全等 → 向量单路居 primary top-1;挂 node「曲率引擎」。
    #  ep_self0002 source_text 含"番茄支架"(连续子串)→ detail FTS 直查命中;
    #              其 overview 同时充当负例的误召回诱饵(query 与之全等 → 必然被召回)。
    eps = [
        Episode(public_id="ep_self0001", overview="星舰曲率航行测试记录",
                summary="曲率航行能量测试", source_text="今天测试了星舰曲率航行的能量曲线,记录在案。",
                salience_tier=2, status="active", created_at="2026-06-20T09:00:00+00:00",
                activated_at="2026-06-20T09:00:00+00:00", nodes=["曲率引擎"],
                highlights=[{"text": "能量曲线", "tag": "术语"}]),
        Episode(public_id="ep_self0002", overview="周末园艺笔记概览",
                summary="番茄搭架", source_text="周末搭好了番茄支架,番茄长势不错。",
                salience_tier=1, status="active", created_at="2026-06-21T09:00:00+00:00",
                activated_at="2026-06-21T09:00:00+00:00"),
    ]
    for ep in eps:
        write_episode(cfg.episodes_dir, ep)
    write_node(cfg.nodes_dir, Node(label="曲率引擎", type="concept",
                                   created_at="t0", updated_at="t0"))
    rep = rebuild(cfg, FakeProvider(model="fake", dim=16))
    assert rep.episodes == 2 and rep.vectors == 2, rep

    # 夹具走文件 + load_queries(顺便验证:# 注释行跳过、无 type 默认 semantic)。
    fixture = [
        {"query": "星舰曲率航行测试记录", "mode": "episode", "expect": ["ep_self0001"],
         "note": "overview 完全一致,fake 向量距离 0,应居 primary top-1(无 type,测默认)"},
        {"query": "番茄支架", "mode": "detail", "type": "verbatim", "expect": ["ep_self0002"],
         "note": "source_text 含连续子串,FTS grep 直查命中"},
        {"query": "曲率引擎", "mode": "concept", "type": "concept", "expect": ["ep_self0001"],
         "note": "node 下唯一 active episode,概念取应命中"},
        {"query": "量子隧穿望远镜观测日志", "mode": "detail", "type": "negative", "expect": [],
         "note": "负例:库内无此逐字词组,FTS 应空手 → 干净"},
        {"query": "周末园艺笔记概览", "mode": "episode", "type": "negative", "expect": [],
         "note": "负例:与 ep_self0002 的 overview 全等,向量必召回 → 误召回"},
    ]
    qpath = Path(tmp) / "queries.jsonl"
    qpath.write_text("# selftest 夹具\n" + "\n".join(
        json.dumps(q, ensure_ascii=False) for q in fixture) + "\n", encoding="utf-8")
    queries = load_queries(qpath)
    assert len(queries) == 5 and queries[0]["type"] == "semantic", queries

    k = cfg.recall.topk_final
    report = evaluate(cfg, queries, k=k, now=now, verbose=True)  # verbose 走一遍打印路径
    print_report(report)

    # 只读断言:evaluate 用 touch=False,时钟不该被刷(仍等于 rebuild 后的 activated_at)
    from memory_system.db.connection import connect
    con = connect(cfg.db_path)
    try:
        clocks = dict(con.execute("SELECT public_id, last_accessed_at FROM episodes"))
    finally:
        con.close()
    assert clocks["ep_self0001"] == "2026-06-20T09:00:00+00:00", clocks
    assert clocks["ep_self0002"] == "2026-06-21T09:00:00+00:00", clocks

    # 旧三条断言(正例口径,不回退):全命中 rank 1、hit 率、覆盖。
    pos = [r for r in report["per_query"] if not r["negative"]]
    assert all(r["best_rank"] == 1 for r in pos), pos
    assert report["hit1_rate"] == 1.0 and report["hitk_rate"] == 1.0, report
    assert report["mean_best_rank"] == 1.0, report
    assert report["coverage"] == "3/3", report

    # 新:MRR(全 rank 1 → 1.0)与正例分母(负例不得拉低 hit 率)。
    assert report["mrr"] == 1.0, report
    assert report["n"] == 5 and report["n_pos"] == 3, report

    # 新:负例指标——干净 1 条、误召回 1 条。
    per = {r["query"]: r for r in report["per_query"]}
    clean_q = per["量子隧穿望远镜观测日志"]
    dirty_q = per["周末园艺笔记概览"]
    assert clean_q["negative"] and clean_q["clean"] and clean_q["n_false"] == 0, clean_q
    assert dirty_q["negative"] and not dirty_q["clean"] and dirty_q["n_false"] >= 1, dirty_q
    # 诱饵必被误召回(名次受衰减乘子影响不锁死:tier 2 的 ep_self0001 激活度更高可能反超)
    assert "ep_self0002" in dirty_q["topk_ids"], dirty_q
    neg = report["negatives"]
    assert neg["n"] == 2 and neg["clean_rate"] == 0.5, neg
    assert neg["mean_false_topk"] == dirty_q["n_false"] / 2, neg

    # 新:分组小结形状与数值。
    bm, bt = report["by_mode"], report["by_type"]
    assert set(bm) == {"episode", "detail", "concept"}, bm
    assert bm["episode"]["n"] == 2 and bm["episode"]["n_pos"] == 1 \
        and bm["episode"]["n_neg"] == 1 and bm["episode"]["mrr"] == 1.0, bm
    assert bm["concept"]["hit1_rate"] == 1.0, bm
    assert set(bt) == {"semantic", "verbatim", "concept", "negative"}, bt
    assert bt["semantic"]["n"] == 1 and bt["semantic"]["mrr"] == 1.0, bt
    assert bt["negative"]["n"] == 2 and bt["negative"]["hit1_rate"] is None \
        and bt["negative"]["neg_clean_rate"] == 0.5, bt

    # 新:--out 落盘(manifest 存在则内联摘要;超长映射折叠成条数)。
    mpath = Path(tmp) / "manifest.json"
    mpath.write_text(json.dumps({
        "total": 2, "anchor": "2026-07-02", "embedding": {"model": "fake", "dim": 16},
        "id_map": {f"mem-{i:02d}": f"ep_syn{i:04d}" for i in range(15)},  # >12 项,应折叠
    }, ensure_ascii=False), encoding="utf-8")
    opath = Path(tmp) / "report.json"
    dump_report(opath, report, cfg.recall, ["w_activation=0.3"], mpath)
    loaded = json.loads(opath.read_text(encoding="utf-8"))
    assert set(loaded) >= {"generated_at", "k", "recall_config", "param_overrides",
                           "summary", "by_mode", "by_type", "per_query", "manifest"}, list(loaded)
    assert loaded["recall_config"]["topk_final"] == cfg.recall.topk_final, loaded["recall_config"]
    assert loaded["param_overrides"] == ["w_activation=0.3"], loaded
    assert loaded["k"] == k and len(loaded["per_query"]) == 5, loaded
    assert loaded["per_query"][0]["topk_ids"] == ["ep_self0001"] \
        or loaded["per_query"][0]["topk_ids"][0] == "ep_self0001", loaded["per_query"][0]
    assert loaded["summary"]["mrr"] == 1.0, loaded["summary"]
    assert loaded["summary"]["negatives"]["clean_rate"] == 0.5, loaded["summary"]
    assert loaded["manifest"]["total"] == 2, loaded["manifest"]
    assert loaded["manifest"]["id_map"] == "(15 项,略)", loaded["manifest"]

    print("[selftest] PASS: 3/3 正例命中且均居 rank 1(MRR=1.0);负例 1 干净 1 误召回;"
          "分组小结与 --out 报告形状正确;touch=False 未刷时钟。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检索评测(hit@k / MRR / 负例误召回),对真实库跑,不进回归。")
    ap.add_argument("--file", type=Path, default=_DEFAULT_QUERIES, help="queries.jsonl 路径")
    ap.add_argument("--k", type=int, default=None, help="hit@k 的 k(默认 = RecallConfig.topk_final)")
    ap.add_argument("--param", action="append", default=[],
                    help="临时覆盖 RecallConfig 字段,如 --param w_activation=0.5(可重复)")
    ap.add_argument("--verbose", action="store_true",
                    help="miss 的正例 / 有误召回的负例打出实际 top-k(public_id + 文本前 40 字)")
    ap.add_argument("--out", type=Path, default=None,
                    help="完整报告落 JSON(RecallConfig 快照 + 每 query 明细),两份 diff 即 A/B")
    ap.add_argument("--selftest", action="store_true", help="离线 fake 自测,不碰真实库")
    args = ap.parse_args(argv)

    if args.selftest:
        return run_selftest()

    from memory_system.config import load_config

    cfg = load_config()
    cfg = replace(cfg, recall=apply_params(cfg.recall, args.param))
    if args.param:
        print(f"[覆盖] {', '.join(args.param)}")
    k = args.k if args.k is not None else cfg.recall.topk_final

    if not args.file.exists():
        raise SystemExit(f"夹具文件不存在: {args.file}")
    queries = load_queries(args.file)
    report = evaluate(cfg, queries, k=k, verbose=args.verbose)
    print_report(report)
    if args.out is not None:
        # manifest 就近找:与夹具同目录(默认即 eval/manifest.json)
        dump_report(args.out, report, cfg.recall, args.param,
                    args.file.parent / "manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
