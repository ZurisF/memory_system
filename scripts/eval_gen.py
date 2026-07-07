"""评测语料生成驱动(eval 步骤①④的自动化):调 mimo 批量生成召回评测的语料与题目。

对应 `eval/README.md` 的两步手工流程:
  ① corpus  —— 按 `eval/clusters.jsonl` 的簇清单分批生成语料,append 到 eval/raw/corpus.jsonl;
  ④ queries —— 读 `eval/raw/query_inputs/<簇>.jsonl`(prep-queries 产物)分批出题,
               append 到 eval/raw/queries_raw.jsonl。

设计要点:
  - API:小米 MiMo,OpenAI 兼容(POST {base}/chat/completions),urllib 直连零依赖;
    key 只从环境变量 `mimo_api_key` 读,缺失时人读报错退出。
  - 便宜模型长输出不可靠,所以 corpus 按 --batch-size(默认 5)分小批;每批的 user 消息
    带明确的本批 id 区间与已用 id 列表,防重复、可断点续跑(已存在的 --out 里同前缀 id 计入已用)。
  - 响应逐行校验,好行 append,坏行落 rejects.log(与 --out 同目录)带原因与原文;
    corpus 批内坏行超半数则对本批缺失的 id 重试一次。
  - transport 可注入(--selftest 用 fake transport 全程离线),真实实现见 urllib_transport。

用法:
    .venv/bin/python scripts/eval_gen.py corpus                       # 全部簇生成语料
    .venv/bin/python scripts/eval_gen.py corpus --cluster mem         # 只生成 mem 簇
    .venv/bin/python scripts/eval_gen.py corpus --dry-run             # 打印将发送的载荷,不调 API
    .venv/bin/python scripts/eval_gen.py queries                      # 全部簇出题
    .venv/bin/python scripts/eval_gen.py queries --cluster mem        # 只对 mem 簇出题
    .venv/bin/python scripts/eval_gen.py --selftest                   # 离线自测(不联网)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

_REPO = Path(__file__).resolve().parent.parent
_CLUSTERS = _REPO / "eval" / "clusters.jsonl"
_PROMPT_CORPUS = _REPO / "eval" / "prompts" / "gen_corpus_system.txt"
_PROMPT_QUERIES = _REPO / "eval" / "prompts" / "gen_queries_system.txt"

BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
KEY_ENV = "mimo_api_key"
DEFAULT_MODEL = "mimo-v2.5"
TIMEOUT_S = 300
RETRIES = 2       # 网络错误/超时重试次数
BACKOFF_S = 5     # 重试退避秒数

# 语料行必须恰好含这 7 个字段(施工书 §1.1)
CORPUS_FIELDS = {"id", "source_text", "overview", "summary", "highlights", "nodes", "salience_tier"}
QUERY_MODES = {"episode", "detail", "concept"}
QUERY_TYPES = {"semantic", "verbatim", "short", "concept", "multi", "negative", "temporal"}

# transport 签名:(payload dict, timeout 秒) -> 响应 JSON dict(OpenAI 形状)
Transport = Callable[[dict, int], dict]


# ---- HTTP 层(可注入)----
def urllib_transport(payload: dict, timeout: int) -> dict:
    """真实 transport:POST {BASE_URL}/chat/completions。外网 HTTPS 走 shell 代理是正常路径。"""
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise SystemExit(f"环境变量 {KEY_ENV} 未设置;key 只从环境读。请 export {KEY_ENV}=<你的 key> 后重跑。")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def call_chat(transport: Transport, system: str, user: str, *,
              model: str, temperature: float) -> str:
    """一次 chat 调用:网络错误/超时重试 RETRIES 次(退避 BACKOFF_S 秒),返回 content 文本。"""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "stream": False,
    }
    last_err: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            data = transport(payload, TIMEOUT_S)
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < RETRIES:
                print(f"  [重试 {attempt + 1}/{RETRIES}] 网络错误: {e},{BACKOFF_S}s 后重试")
                time.sleep(BACKOFF_S)
    else:
        raise SystemExit(f"API 调用失败(重试 {RETRIES} 次后放弃): {last_err}")
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SystemExit(f"响应缺 choices[0].message.content: {str(data)[:300]}")
    if not isinstance(text, str) or not text.strip():
        raise SystemExit("响应 content 为空")
    return text


def strip_fences(text: str) -> str:
    """剥掉 markdown 围栏:模型偶尔无视指令包一层 ```jsonl ... ```,剥掉后再逐行解析。"""
    s = text.strip()
    m = re.match(r"^```[\w-]*\s*\n(.*?)\n?```\s*$", s, flags=re.DOTALL)
    return m.group(1) if m else s


# ---- 公共小件 ----
def load_clusters(path: Path, only: str | None = None) -> list[dict]:
    """读簇清单 jsonl;--cluster 过滤时前缀必须存在,否则人读报错。"""
    if not path.exists():
        raise SystemExit(f"簇清单不存在: {path}")
    clusters = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    if only is not None:
        clusters = [c for c in clusters if c["prefix"] == only]
        if not clusters:
            raise SystemExit(f"簇清单里没有前缀 {only!r}(见 {path})")
    return clusters


def collect_used_ids(out_path: Path, prefix: str) -> set[str]:
    """断点续跑:已存在的 --out 里同前缀的 id 计入已用(坏 JSON 行忽略,validate 阶段另管)。"""
    used: set[str] = set()
    if not out_path.exists():
        return used
    for line in out_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        i = obj.get("id")
        if isinstance(i, str) and i.startswith(prefix + "-"):
            used.add(i)
    return used


def append_lines(path: Path, objs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for obj in objs:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def log_rejects(rej_path: Path, batch_tag: str, rejects: list[tuple[str, str]]) -> None:
    """坏行落盘:每条带批次标签、拒收原因、原文。"""
    if not rejects:
        return
    rej_path.parent.mkdir(parents=True, exist_ok=True)
    with rej_path.open("a", encoding="utf-8") as f:
        for reason, raw in rejects:
            f.write(f"# [{batch_tag}] {reason}\n{raw}\n")


# ---- corpus:生成语料 ----
def build_corpus_user(cluster: dict, batch_ids: list[str], used: set[str]) -> str:
    """本批的 user 消息:簇规格 + 明确的本批 id 区间 + 已用 id 列表(防重复)。"""
    neighbor = cluster.get("neighbor") or "独立簇,无近邻,内容自成领域即可"
    ids_str = "、".join(batch_ids)
    used_str = "、".join(sorted(used)) if used else "(无)"
    return (f"簇前缀:{cluster['prefix']}\n"
            f"主题:{cluster['topic']}\n"
            f"条数:{len(batch_ids)}(仅本批)\n"
            f"近邻关系:{neighbor}\n"
            f"本批生成 {batch_ids[0]} 到 {batch_ids[-1]},即恰好这些 id:{ids_str}。\n"
            f"以下 id 已在之前批次用过,禁止再次出现:{used_str}")


def validate_corpus_line(raw: str, prefix: str, used: set[str],
                         source_texts: dict[str, str]) -> tuple[dict | None, str | None]:
    """校验一行语料;返回 (对象, None) 或 (None, 拒收原因)。source_texts 供去重诊断不用,预留。"""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"JSON 不合法: {e}"
    if not isinstance(obj, dict):
        return None, "不是 JSON 对象"
    missing = CORPUS_FIELDS - obj.keys()
    if missing:
        return None, f"缺字段: {sorted(missing)}"
    i = obj["id"]
    if not isinstance(i, str) or not i.startswith(prefix + "-"):
        return None, f"id 前缀不是 {prefix}-: {i!r}"
    if i in used:
        return None, f"id 已用过(重复): {i}"
    src = obj.get("source_text")
    if not isinstance(src, str) or not src:
        return None, "source_text 不是非空字符串"
    hl = obj.get("highlights")
    if not isinstance(hl, list):
        return None, "highlights 不是数组"
    for h in hl:
        if not isinstance(h, dict) or not isinstance(h.get("text"), str):
            return None, f"highlight 形状不对: {h!r}"
        if h["text"] not in src:
            return None, f"highlight 不是 source_text 逐字子串: {h['text']!r}"
    tier = obj.get("salience_tier")
    if tier not in (1, 2, 3):
        return None, f"salience_tier 必须是 1/2/3: {tier!r}"
    return obj, None


def run_corpus_batch(transport: Transport, system: str, cluster: dict,
                     batch_ids: list[str], used: set[str], out_path: Path,
                     rej_path: Path, model: str, batch_tag: str) -> tuple[int, int]:
    """跑一批(含坏行超半数重试一次);返回 (好行数, 坏行数)。好行即时 append 并计入 used。"""
    total_good = total_bad = 0
    want = list(batch_ids)
    for attempt in ("首跑", "重试"):
        user = build_corpus_user(cluster, want, used)
        text = strip_fences(call_chat(transport, system, user,
                                      model=model, temperature=0.8))
        lines = [ln for ln in text.splitlines() if ln.strip()]
        goods: list[dict] = []
        rejects: list[tuple[str, str]] = []
        for ln in lines:
            obj, reason = validate_corpus_line(ln, cluster["prefix"], used, {})
            if obj is None:
                rejects.append((reason or "未知原因", ln))
            else:
                goods.append(obj)
                used.add(obj["id"])
        append_lines(out_path, goods)
        log_rejects(rej_path, f"{batch_tag}/{attempt}", rejects)
        total_good += len(goods)
        total_bad += len(rejects)
        # 坏行超半数(含空响应)才重试;只补本批还缺的 id
        want = [i for i in want if i not in used]
        need_retry = (not lines or len(rejects) > len(lines) / 2) and want
        if attempt == "首跑" and need_retry:
            print(f"  [{batch_tag}] 坏行超半数({len(rejects)}/{len(lines)}),重试缺失 id: {'、'.join(want)}")
            continue
        break
    return total_good, total_bad


def cmd_corpus(args, transport: Transport) -> int:
    system = _PROMPT_CORPUS.read_text(encoding="utf-8")
    clusters = load_clusters(args.clusters, args.cluster)
    rej_path = args.out.parent / "rejects.log"
    for cluster in clusters:
        prefix, n = cluster["prefix"], int(cluster["n"])
        used = collect_used_ids(args.out, prefix)
        todo = [f"{prefix}-{i:02d}" for i in range(1, n + 1) if f"{prefix}-{i:02d}" not in used]
        if not todo:
            print(f"[{prefix}] {n} 条已齐,跳过(断点续跑)")
            continue
        batches = [todo[i:i + args.batch_size] for i in range(0, len(todo), args.batch_size)]
        print(f"[{prefix}] 目标 {n} 条,已有 {len(used)},待生成 {len(todo)}(分 {len(batches)} 批)")
        for bi, batch_ids in enumerate(batches, start=1):
            tag = f"{prefix} 批{bi}/{len(batches)}"
            if args.dry_run:
                print(f"--- dry-run:{tag} 将发送的 user 载荷 ---")
                print(build_corpus_user(cluster, batch_ids, used))
                continue
            good, bad = run_corpus_batch(transport, system, cluster, batch_ids,
                                         used, args.out, rej_path, args.model, tag)
            print(f"  [{tag}] 好行 {good} / 坏行 {bad}")
    if not args.dry_run:
        print(f"完成。产物: {args.out};坏行(如有): {rej_path}")
    return 0


# ---- queries:出题 ----
def validate_query_line(raw: str, group_ids: set[str]) -> tuple[dict | None, str | None]:
    """校验一行题目;expect 只能引用本组内的 id,negative 的 expect 必须为空。"""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"JSON 不合法: {e}"
    if not isinstance(obj, dict):
        return None, "不是 JSON 对象"
    if not isinstance(obj.get("query"), str) or not obj["query"].strip():
        return None, "query 不是非空字符串"
    if obj.get("mode") not in QUERY_MODES:
        return None, f"mode 不合法: {obj.get('mode')!r}"
    if obj.get("type") not in QUERY_TYPES:
        return None, f"type 不合法: {obj.get('type')!r}"
    expect = obj.get("expect")
    if not isinstance(expect, list):
        return None, "expect 不是数组"
    if obj["type"] == "negative" and expect:
        return None, f"negative 的 expect 必须为空: {expect!r}"
    bad = [e for e in expect if e not in group_ids]
    if bad:
        return None, f"expect 引用了本组外的 id: {bad!r}"
    return obj, None


def cmd_queries(args, transport: Transport) -> int:
    system = _PROMPT_QUERIES.read_text(encoding="utf-8")
    if not args.inputs_dir.is_dir():
        raise SystemExit(f"出题输入目录不存在: {args.inputs_dir}(先跑 eval_ingest.py prep-queries --by-cluster)")
    files = sorted(args.inputs_dir.glob("*.jsonl"))
    if args.cluster is not None:
        files = [f for f in files if f.stem == args.cluster]
        if not files:
            raise SystemExit(f"{args.inputs_dir} 下没有簇文件 {args.cluster}.jsonl")
    if not files:
        raise SystemExit(f"{args.inputs_dir} 下没有 .jsonl 文件")
    rej_path = args.out.parent / "rejects.log"
    for fp in files:
        lines = [ln for ln in fp.read_text(encoding="utf-8").splitlines() if ln.strip()]
        # 每批 ≤5 条记忆:user = 该组 JSONL 原文(不重排、不改写,原样喂)
        groups = [lines[i:i + 5] for i in range(0, len(lines), 5)]
        print(f"[{fp.stem}] {len(lines)} 条记忆,分 {len(groups)} 组出题")
        for gi, group in enumerate(groups, start=1):
            tag = f"{fp.stem} 组{gi}/{len(groups)}"
            group_ids = set()
            for ln in group:
                try:
                    group_ids.add(json.loads(ln)["id"])
                except (json.JSONDecodeError, KeyError):
                    raise SystemExit(f"{fp} 内有坏行(无 id),先修输入文件: {ln[:120]}")
            user = "\n".join(group)
            if args.dry_run:
                print(f"--- dry-run:{tag} 将发送的 user 载荷 ---")
                print(user)
                continue
            text = strip_fences(call_chat(transport, system, user,
                                          model=args.model, temperature=0.7))
            goods: list[dict] = []
            rejects: list[tuple[str, str]] = []
            for ln in (l for l in text.splitlines() if l.strip()):
                obj, reason = validate_query_line(ln, group_ids)
                if obj is None:
                    rejects.append((reason or "未知原因", ln))
                else:
                    goods.append(obj)
            append_lines(args.out, goods)
            log_rejects(rej_path, tag, rejects)
            print(f"  [{tag}] 好行 {len(goods)} / 坏行 {len(rejects)}")
    if not args.dry_run:
        print(f"完成。产物: {args.out};坏行(如有): {rej_path}")
    return 0


# ---- 离线自测(fake transport,不联网)----
def _fake_transport(responses: list[str]) -> Transport:
    """按调用顺序弹出预置响应文本,包成 OpenAI 形状。"""
    queue = list(responses)

    def transport(payload: dict, timeout: int) -> dict:
        assert queue, "fake transport 响应耗尽(调用次数超预期)"
        return {"choices": [{"message": {"content": queue.pop(0)}}],
                "model": payload["model"], "usage": {}}
    return transport


def run_selftest() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="memsys_evalgen_"))
    print(f"[selftest] 临时目录: {tmp}")

    # 夹具簇清单:一簇 4 条
    clusters_path = tmp / "clusters.jsonl"
    clusters_path.write_text(json.dumps(
        {"prefix": "mem", "topic": "测试簇", "n": 4, "neighbor": None},
        ensure_ascii=False) + "\n", encoding="utf-8")

    # 断点续跑:预置 mem-01,应计入已用
    out_path = tmp / "corpus.jsonl"
    seed = {"id": "mem-01", "source_text": "x", "overview": "o", "summary": "s",
            "highlights": [], "nodes": [], "salience_tier": 2}
    out_path.write_text(json.dumps(seed, ensure_ascii=False) + "\n", encoding="utf-8")
    assert collect_used_ids(out_path, "mem") == {"mem-01"}, "已用 id 收集失败"
    print("[selftest] 断点续跑已用 id 收集 ✓")

    # 剥围栏
    assert strip_fences("```jsonl\n{\"a\":1}\n```") == '{"a":1}', "围栏未剥净"
    assert strip_fences('{"a":1}') == '{"a":1}', "无围栏文本被误伤"
    print("[selftest] markdown 围栏剥离 ✓")

    def mk(i: str, src: str = "[我] 原文内容甲。\n[Claude] 回答乙。") -> str:
        return json.dumps({"id": i, "source_text": src, "overview": f"{i} 总览",
                           "summary": "起点发展核心落点", "highlights": [{"text": "原文内容甲", "tag": "事实"}],
                           "nodes": [{"label": "测试概念", "type": "concept", "aliases": []}],
                           "salience_tier": 2}, ensure_ascii=False)

    # corpus:首跑响应带围栏,含 好1(mem-02)+ 坏1(highlight 非子串)+ 重复1(mem-01);
    # 3 行中 2 坏 > 半数 → 触发重试,重试响应补齐 mem-03、mem-04。
    bad_hl = json.dumps({"id": "mem-03", "source_text": "短文本", "overview": "o", "summary": "s",
                         "highlights": [{"text": "不存在的子串", "tag": "事实"}],
                         "nodes": [], "salience_tier": 2}, ensure_ascii=False)
    first = "```jsonl\n" + "\n".join([mk("mem-02"), bad_hl, mk("mem-01")]) + "\n```"
    second = mk("mem-03") + "\n" + mk("mem-04")
    transport = _fake_transport([first, second])

    args = argparse.Namespace(clusters=clusters_path, cluster="mem", model="fake-model",
                              batch_size=5, out=out_path, dry_run=False)
    cmd_corpus(args, transport)

    got = [json.loads(l)["id"] for l in out_path.read_text(encoding="utf-8").splitlines()]
    assert got == ["mem-01", "mem-02", "mem-03", "mem-04"], f"corpus 产物 id 不对: {got}"
    rej = (tmp / "rejects.log").read_text(encoding="utf-8")
    assert "不是 source_text 逐字子串" in rej, f"坏行未进 rejects: {rej}"
    assert "已用过(重复): mem-01" in rej, f"重复 id 未拒收: {rej}"
    print("[selftest] corpus:坏行进 rejects、重复 id 拒收、超半数重试补齐 ✓")

    # queries:2 条记忆一组;响应含 好1 + expect 越界1 + negative 带 expect 1
    inputs_dir = tmp / "query_inputs"
    inputs_dir.mkdir()
    (inputs_dir / "mem.jsonl").write_text("\n".join(
        json.dumps({"id": i, "source_text": "原文", "nodes": ["测试概念"]}, ensure_ascii=False)
        for i in ("mem-01", "mem-02")) + "\n", encoding="utf-8")
    q_good = json.dumps({"query": "之前那个测试聊了啥", "mode": "episode", "expect": ["mem-01"],
                         "type": "semantic", "note": "换说法"}, ensure_ascii=False)
    q_oob = json.dumps({"query": "越界题", "mode": "episode", "expect": ["mem-99"],
                        "type": "semantic", "note": "expect 越界"}, ensure_ascii=False)
    q_badneg = json.dumps({"query": "负例带 expect", "mode": "episode", "expect": ["mem-01"],
                           "type": "negative", "note": "应拒收"}, ensure_ascii=False)
    q_out = tmp / "queries_raw.jsonl"
    qargs = argparse.Namespace(cluster=None, model="fake-model", inputs_dir=inputs_dir,
                               out=q_out, dry_run=False)
    cmd_queries(qargs, _fake_transport(["\n".join([q_good, q_oob, q_badneg])]))

    q_lines = [json.loads(l) for l in q_out.read_text(encoding="utf-8").splitlines()]
    assert len(q_lines) == 1 and q_lines[0]["expect"] == ["mem-01"], f"queries 产物不对: {q_lines}"
    rej = (tmp / "rejects.log").read_text(encoding="utf-8")
    assert "本组外的 id: ['mem-99']" in rej, f"expect 越界未拒收: {rej}"
    assert "negative 的 expect 必须为空" in rej, f"negative 带 expect 未拒收: {rej}"
    print("[selftest] queries:expect 越界拒收、negative 带 expect 拒收 ✓")

    print("[selftest] PASS: 围栏剥离 / 坏行落 rejects / 重复 id 拒收 / 断点续跑 / "
          "超半数重试 / queries 越界拒收,全部通过(全程离线)。")
    return 0


# ---- 入口 ----
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="调 mimo 批量生成召回评测语料(corpus)与题目(queries),对应 eval/README.md ①④。")
    ap.add_argument("--selftest", action="store_true", help="离线自测(fake transport,不联网)")
    sub = ap.add_subparsers(dest="cmd")

    sc = sub.add_parser("corpus", help="按簇清单分批生成语料,append 到 --out")
    sc.add_argument("--cluster", default=None, help="只生成该前缀的簇(缺省=清单里全部簇)")
    sc.add_argument("--clusters", type=Path, default=_CLUSTERS, help="簇清单 jsonl 路径")
    sc.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名(默认 {DEFAULT_MODEL},可换 mimo-v2.5-pro)")
    sc.add_argument("--batch-size", type=int, default=5, help="每批条数(便宜模型长输出不可靠,默认 5)")
    sc.add_argument("--out", type=Path, default=_REPO / "eval" / "raw" / "corpus.jsonl",
                    help="产物路径(append;已有同前缀 id 计入已用,断点续跑)")
    sc.add_argument("--dry-run", action="store_true", help="打印将发送的 user 载荷,不调 API")

    sq = sub.add_parser("queries", help="读 query_inputs 分批出题,append 到 --out")
    sq.add_argument("--cluster", default=None, help="只对该簇文件出题(缺省=目录下全部)")
    sq.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名(默认 {DEFAULT_MODEL})")
    sq.add_argument("--inputs-dir", type=Path, default=_REPO / "eval" / "raw" / "query_inputs",
                    help="prep-queries 产物目录")
    sq.add_argument("--out", type=Path, default=_REPO / "eval" / "raw" / "queries_raw.jsonl",
                    help="产物路径(append)")
    sq.add_argument("--dry-run", action="store_true", help="打印将发送的 user 载荷,不调 API")

    args = ap.parse_args(argv)
    if args.selftest:
        return run_selftest()
    if args.cmd == "corpus":
        return cmd_corpus(args, urllib_transport)
    if args.cmd == "queries":
        return cmd_queries(args, urllib_transport)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
