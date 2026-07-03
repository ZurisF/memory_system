"""检索评测夹具(S6-8):对真实库跑手标 query,算 hit@1 / hit@k / 期望条目平均名次。

这步是"跑数据找感觉"类问题(overview 写法、topk、w_activation)的度量前提。
**对真实库跑(不设 fake 环境变量),不进 verify 回归。**

夹具文件 `eval/queries.jsonl` 每行一个 JSON 对象(# 注释行 / 空行跳过):
    {"query": "...", "mode": "episode|detail|concept", "expect": ["ep_xxxx"], "note": "..."}
  - query   检索词;concept 模式下是 node 的 label 或别名。
  - mode    episode(向量+FTS 双路,排名取 primary 主槽)/ detail(FTS grep,取 hits)/
            concept(膜 join,取 episodes);可选 "context" 字段仅 concept 用。
  - expect  期望命中的 episode public_id 列表。
  - note    人读理由,脚本忽略。

评测口径:
  - 每条 query 跑对应 recall,**touch=False**(只读、不刷时钟,evaluate 不污染衰减态),
    且不走重构(拿结构化候选集直接看名次)。
  - 把结果排成一列 public_id(episode=primary 主槽;detail=hits;concept=episodes),
    对每个 expect 求其 1-based 名次;best_rank = 最靠前的命中名次。
  - hit@1 = best_rank==1;hit@k = best_rank<=k(k 默认 = RecallConfig.topk_final,--k 覆盖);
    mean_best_rank = 命中 query 的 best_rank 均值。

用法:
    .venv/bin/python scripts/eval_recall.py                       # 跑 eval/queries.jsonl
    .venv/bin/python scripts/eval_recall.py --file /tmp/q.jsonl   # 指定夹具(临时库测试用)
    .venv/bin/python scripts/eval_recall.py --k 5                 # 改 hit@k 的 k
    .venv/bin/python scripts/eval_recall.py --param w_activation=0.5 --param topk_final=5  # A/B 临时覆盖
    .venv/bin/python scripts/eval_recall.py --selftest            # 离线 fake 自测(不碰真实库)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_QUERIES = _REPO / "eval" / "queries.jsonl"


# ---- 夹具读取 ----
def load_queries(path: Path) -> list[dict]:
    """读 jsonl,跳过 # 注释行与空行;基本字段校验。"""
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
def ranked_ids(result: dict, mode: str) -> list[str]:
    if mode == "detail":
        return [h["public_id"] for h in result["hits"]]
    if mode == "episode":
        return [p["public_id"] for p in result["slots"]["primary"]]
    if mode == "concept":
        return [e["public_id"] for e in result["episodes"]]
    raise ValueError(f"未知 mode: {mode}")


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
def evaluate(cfg, queries: list[dict], k: int, now: datetime | None = None) -> dict:
    per: list[dict] = []
    for q in queries:
        res = run_query(cfg, q, now)
        ids = ranked_ids(res, q["mode"])
        expect = q.get("expect") or []
        ranks = [ids.index(e) + 1 if e in ids else None for e in expect]
        found = [r for r in ranks if r is not None]
        best = min(found) if found else None
        per.append({
            "query": q["query"], "mode": q["mode"],
            "n_expect": len(expect), "n_found": len(found),
            "best_rank": best,
            "hit1": best == 1,
            "hitk": best is not None and best <= k,
        })
    n = len(per)
    best_ranks = [r["best_rank"] for r in per if r["best_rank"] is not None]
    return {
        "per_query": per,
        "n": n,
        "k": k,
        "hit1_rate": sum(r["hit1"] for r in per) / n if n else 0.0,
        "hitk_rate": sum(r["hitk"] for r in per) / n if n else 0.0,
        "mean_best_rank": sum(best_ranks) / len(best_ranks) if best_ranks else None,
        "coverage": f"{len(best_ranks)}/{n}",
    }


def print_report(report: dict) -> None:
    k = report["k"]
    per = report["per_query"]
    if not per:
        print("(夹具为空:eval/queries.jsonl 尚无 query,先添加手标条目再评测。)")
        return
    qw = min(40, max(12, *(len(r["query"]) for r in per)))
    header = f"{'query':<{qw}}  {'mode':<8} {'expect':>6} {'rank':>5} {'hit@1':>6} {'hit@'+str(k):>6}"
    print(header)
    print("-" * len(header))
    for r in per:
        q = r["query"] if len(r["query"]) <= qw else r["query"][: qw - 1] + "…"
        rank = "-" if r["best_rank"] is None else str(r["best_rank"])
        print(f"{q:<{qw}}  {r['mode']:<8} {r['n_expect']:>6} {rank:>5} "
              f"{('✓' if r['hit1'] else '·'):>6} {('✓' if r['hitk'] else '·'):>6}")
    print("-" * len(header))
    mbr = report["mean_best_rank"]
    print(f"query 数={report['n']}  hit@1={report['hit1_rate']:.3f}  "
          f"hit@{k}={report['hitk_rate']:.3f}  "
          f"平均名次={'n/a' if mbr is None else f'{mbr:.2f}'}  覆盖={report['coverage']}")


# ---- 离线 fake 自测(不碰真实库)----
def run_selftest() -> int:
    """临时库 + 已知 query,验证报表数字正确。设 fake 环境变量后再 load_config,全程离线。"""
    import tempfile
    from datetime import timezone

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
    #  ep_self0002 source_text 含"番茄支架"(连续子串)→ detail FTS 直查命中。
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

    queries = [
        {"query": "星舰曲率航行测试记录", "mode": "episode", "expect": ["ep_self0001"],
         "note": "overview 完全一致,fake 向量距离 0,应居 primary top-1"},
        {"query": "番茄支架", "mode": "detail", "expect": ["ep_self0002"],
         "note": "source_text 含连续子串,FTS grep 直查命中"},
        {"query": "曲率引擎", "mode": "concept", "expect": ["ep_self0001"],
         "note": "node 下唯一 active episode,概念取应命中"},
    ]
    k = cfg.recall.topk_final
    report = evaluate(cfg, queries, k=k, now=now)
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

    per = report["per_query"]
    assert all(r["best_rank"] == 1 for r in per), per
    assert report["hit1_rate"] == 1.0 and report["hitk_rate"] == 1.0, report
    assert report["mean_best_rank"] == 1.0, report
    assert report["coverage"] == "3/3", report
    print("[selftest] PASS: 3/3 query 命中且期望条目均居 rank 1;touch=False 未刷时钟。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检索评测(hit@k),对真实库跑,不进回归。")
    ap.add_argument("--file", type=Path, default=_DEFAULT_QUERIES, help="queries.jsonl 路径")
    ap.add_argument("--k", type=int, default=None, help="hit@k 的 k(默认 = RecallConfig.topk_final)")
    ap.add_argument("--param", action="append", default=[],
                    help="临时覆盖 RecallConfig 字段,如 --param w_activation=0.5(可重复)")
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
    report = evaluate(cfg, queries, k=k)
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
