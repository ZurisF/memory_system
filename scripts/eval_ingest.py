"""召回评测语料落库工具链(eval 步骤 2):校验 / 出题输入 / 落库 / 清库。

配套 `project/eval_build_plan.md` 的评测流水线:mimo 批量生成的语料
(`eval/raw/corpus.jsonl`,契约见施工书 §1.1)与题目(`eval/raw/queries_raw.jsonl`,§1.2)
经本工具校验后落成碎片正本 + 重建索引,产出正式夹具 `eval/queries.jsonl` 与
溯源锚 `eval/manifest.json`。**直接操作真库**(裁定 1:评测完 reset 即可,不建独立库)。

子命令:
  validate <corpus.jsonl> [queries_raw.jsonl]
      只校验不落盘:JSON 合法 / 字段齐 / id 批内唯一 / highlights 逐字子串 /
      tier∈1–3 / nodes 形状 / query 的 mode、type 合法 / expect 引用的临时 id 存在
      (negative 的空 expect 除外)。报错带行号,**一次报全不 fail-fast**。
  prep-queries <corpus.jsonl> [--by-cluster] [--outdir DIR]
      输出出题用户消息载荷:每行只含 {"id","source_text","nodes"(label 列表)},
      **绝不含 overview/summary**(裁定 4:overview 是 embedding 对象,出题器看到
      就是把答案抄进考题)。默认打到 stdout;--by-cluster 按 id 前缀(首个连字符前)
      分文件落 eval/raw/query_inputs/<簇>.jsonl。
  ingest <corpus.jsonl> <queries_raw.jsonl> [--anchor YYYY-MM-DD]
      先跑 validate;然后:临时 id → ep_syn+4 位行序号(0001 起)→ 时间戳按行序
      均匀铺到 anchor(默认今天 UTC)往前 365 天(首条最旧,裁定 3,确定性可复现)
      → 写碎片(node 已存在则并别名重写,不重复建)→ index.rebuild(真 embedding,
      联网耗额度)→ 改写 expect 落 eval/queries.jsonl + eval/manifest.json。
      中途失败不回滚:碎片已写的部分留在库里,重跑 `reset` 后再来(评测库可弃)。
  reset [--yes]
      fragments/episodes|nodes 全量**移动**到 <home>/backup_<UTC时间戳>/ 再重建空索引。
      绝不删除文件;无 --yes 先打印将移动的条数并交互确认。

用法:
    .venv/bin/python scripts/eval_ingest.py validate eval/raw/corpus.jsonl eval/raw/queries_raw.jsonl
    .venv/bin/python scripts/eval_ingest.py prep-queries eval/raw/corpus.jsonl --by-cluster
    .venv/bin/python scripts/eval_ingest.py ingest eval/raw/corpus.jsonl eval/raw/queries_raw.jsonl
    .venv/bin/python scripts/eval_ingest.py reset
    .venv/bin/python scripts/eval_ingest.py --selftest      # 离线 fake 自测,不碰真实库
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_QUERY_INPUTS = _REPO / "eval" / "raw" / "query_inputs"
_DEFAULT_QUERIES_OUT = _REPO / "eval" / "queries.jsonl"
_DEFAULT_MANIFEST_OUT = _REPO / "eval" / "manifest.json"

_DAYS_SPAN = 365  # 时间戳铺开跨度(裁定 3)
_CORPUS_FIELDS = {"id", "source_text", "overview", "summary", "highlights", "nodes", "salience_tier"}
_QUERY_FIELDS = {"query", "mode", "expect", "type", "note", "context"}
_NODE_TYPES = ("concept", "entity", "project", "person")
_QUERY_MODES = ("episode", "detail", "concept")
_QUERY_TYPES = ("semantic", "verbatim", "short", "concept", "multi", "negative", "temporal")
_HL_TAGS = ("术语", "金句", "决定", "事实")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_jsonl(path: Path):
    """逐行 yield (行号, 去空白后的内容);跳过空行与 # 注释行(与 eval_recall 夹具惯例一致)。"""
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        yield lineno, s


# ---- 校验(一次报全,不 fail-fast)----


def validate_corpus(path: Path) -> tuple[list[dict], list[str], list[str]]:
    """校验语料文件,返回 (记录列表(按文件行序), errors, warnings)。

    记录只要 JSON 可解析且是对象就进列表(供报错继续);有 errors 时上游禁止落盘。
    """
    errors: list[str] = []
    warnings: list[str] = []
    records: list[dict] = []
    if not path.exists():
        return [], [f"{path}: 文件不存在"], []
    seen_ids: dict[str, int] = {}          # id → 首见行号(查批内重复)
    alias_owner: dict[str, tuple[str, int]] = {}  # alias → (label, 行号):别名全库唯一,撞了 rebuild 必炸

    for lineno, s in _iter_jsonl(path):
        loc = f"{path.name}:{lineno}"
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            errors.append(f"{loc} 不是合法 JSON: {e}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"{loc} 每行必须是 JSON 对象")
            continue
        records.append(obj)

        extra = set(obj) - _CORPUS_FIELDS
        if extra:
            warnings.append(f"{loc} 未知字段将被忽略: {sorted(extra)}")

        # id:批内唯一;簇前缀 = 首个连字符前
        cid = obj.get("id")
        if not isinstance(cid, str) or not cid.strip():
            errors.append(f"{loc} id 缺失或非法(需非空字符串)")
        else:
            if cid in seen_ids:
                errors.append(f"{loc} id {cid!r} 批内重复(首见 {path.name}:{seen_ids[cid]})")
            else:
                seen_ids[cid] = lineno
            if "-" not in cid:
                warnings.append(f"{loc} id {cid!r} 无连字符,--by-cluster 会把整个 id 当簇名")

        # 三段 prose 必须非空
        for key in ("source_text", "overview", "summary"):
            v = obj.get(key)
            if not isinstance(v, str) or not v.strip():
                errors.append(f"{loc} {key} 缺失或为空")
        src = obj.get("source_text") if isinstance(obj.get("source_text"), str) else ""

        # highlights:0–3 条,text 必须是 source_text 的逐字连续子串
        hls = obj.get("highlights")
        if not isinstance(hls, list):
            errors.append(f"{loc} highlights 必须是数组(可为空 [])")
        else:
            if len(hls) > 3:
                errors.append(f"{loc} highlights 超过 3 条({len(hls)} 条)")
            for j, hl in enumerate(hls):
                if not isinstance(hl, dict) or not isinstance(hl.get("text"), str) or not hl["text"]:
                    errors.append(f"{loc} highlights[{j}] 形状非法(需 {{\"text\",\"tag\"}},text 非空)")
                    continue
                if hl["text"] not in src:
                    errors.append(f"{loc} highlights[{j}].text 不是 source_text 的逐字子串: {hl['text'][:40]!r}")
                tag = hl.get("tag")
                if tag is not None and tag not in _HL_TAGS:
                    warnings.append(f"{loc} highlights[{j}].tag {tag!r} 不在 {'/'.join(_HL_TAGS)} 内")

        # nodes:1–3 个;label 非空且不含换行(碎片 frontmatter 是逐行格式)
        nds = obj.get("nodes")
        if not isinstance(nds, list) or not (1 <= len(nds) <= 3):
            errors.append(f"{loc} nodes 必须是 1–3 个对象的数组")
        else:
            for j, nd in enumerate(nds):
                if not isinstance(nd, dict):
                    errors.append(f"{loc} nodes[{j}] 必须是对象")
                    continue
                label = nd.get("label")
                if not isinstance(label, str) or not label.strip():
                    errors.append(f"{loc} nodes[{j}].label 缺失或为空")
                    continue
                if "\n" in label or "\r" in label:
                    errors.append(f"{loc} nodes[{j}].label 含换行,碎片 frontmatter 无法承载")
                t = nd.get("type")
                if t is not None and t not in _NODE_TYPES:
                    errors.append(f"{loc} nodes[{j}].type {t!r} 非法(concept/entity/project/person/null)")
                aliases = nd.get("aliases", [])
                if not isinstance(aliases, list) or any(
                    not isinstance(a, str) or not a.strip() for a in aliases
                ):
                    errors.append(f"{loc} nodes[{j}].aliases 必须是非空字符串数组")
                    continue
                for a in aliases:
                    if "\n" in a or "\r" in a:
                        errors.append(f"{loc} nodes[{j}] 别名 {a!r} 含换行,碎片 frontmatter 无法承载")
                        continue
                    if a == label:
                        warnings.append(f"{loc} nodes[{j}] 别名与 label 相同: {a!r}")
                        continue
                    owner = alias_owner.get(a)
                    if owner is None:
                        alias_owner[a] = (label, lineno)
                    elif owner[0] != label:
                        errors.append(
                            f"{loc} 别名 {a!r} 已被 node {owner[0]!r}(第 {owner[1]} 行)占用"
                            "——别名全库唯一,rebuild 会撞 UNIQUE"
                        )

        # salience_tier ∈ 1–3(bool 是 int 子类,单独挡)
        tier = obj.get("salience_tier")
        if isinstance(tier, bool) or not isinstance(tier, int) or tier not in (1, 2, 3):
            errors.append(f"{loc} salience_tier 必须是整数 1/2/3: {tier!r}")

    if not records and not errors:
        errors.append(f"{path.name}: 语料为空(无有效行)")
    return records, errors, warnings


def validate_queries(path: Path, corpus_ids: set[str]) -> tuple[list[dict], list[str], list[str]]:
    """校验 queries_raw 文件,返回 (记录列表, errors, warnings)。

    expect 引用不存在的临时 id 是错误;negative 必须空 expect,非 negative 必须非空。
    """
    errors: list[str] = []
    warnings: list[str] = []
    records: list[dict] = []
    if not path.exists():
        return [], [f"{path}: 文件不存在"], []

    for lineno, s in _iter_jsonl(path):
        loc = f"{path.name}:{lineno}"
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            errors.append(f"{loc} 不是合法 JSON: {e}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"{loc} 每行必须是 JSON 对象")
            continue
        records.append(obj)

        extra = set(obj) - _QUERY_FIELDS
        if extra:
            warnings.append(f"{loc} 未知字段将被忽略: {sorted(extra)}")

        q = obj.get("query")
        if not isinstance(q, str) or not q.strip():
            errors.append(f"{loc} query 缺失或为空")
        mode = obj.get("mode")
        if mode not in _QUERY_MODES:
            errors.append(f"{loc} mode 必须是 {'/'.join(_QUERY_MODES)}: {mode!r}")
        qtype = obj.get("type")
        if qtype is None:
            warnings.append(f"{loc} 缺 type 字段,评测将按 semantic 分组")
        elif qtype not in _QUERY_TYPES:
            errors.append(f"{loc} type 必须是 {'/'.join(_QUERY_TYPES)}: {qtype!r}")

        expect = obj.get("expect")
        if not isinstance(expect, list) or any(not isinstance(e, str) for e in expect):
            errors.append(f"{loc} expect 必须是字符串数组")
        else:
            if qtype == "negative":
                if expect:
                    errors.append(f"{loc} negative 的 expect 必须为空数组: {expect!r}")
            elif not expect:
                errors.append(f"{loc} expect 为空(只有 negative 允许空 expect)")
            for e in expect:
                if e not in corpus_ids:
                    errors.append(f"{loc} expect 引用了语料中不存在的临时 id: {e!r}")

        if "context" in obj and mode != "concept":
            warnings.append(f"{loc} context 仅 concept 模式使用,评测会忽略")
        note = obj.get("note")
        if note is not None and not isinstance(note, str):
            warnings.append(f"{loc} note 非字符串,落夹具时将被丢弃")
    return records, errors, warnings


def _report_issues(errors: list[str], warnings: list[str]) -> None:
    for w in warnings:
        print(f"[警告] {w}")
    for e in errors:
        print(f"[错误] {e}")


# ---- prep-queries:出题输入载荷(绝不含 overview/summary,裁定 4)----


def build_query_inputs(records: list[dict]) -> list[dict]:
    """每条语料 → {"id","source_text","nodes"(label 列表)};白名单构造,防泄漏。"""
    return [
        {
            "id": r["id"],
            "source_text": r["source_text"],
            "nodes": [nd["label"] for nd in r["nodes"]],
        }
        for r in records
    ]


def _cluster_of(cid: str) -> str:
    """簇名 = id 首个连字符前的前缀;无连字符则整个 id。用于 --by-cluster 分文件。"""
    return cid.split("-", 1)[0]


def _safe_cluster_filename(cluster: str) -> str:
    return re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", cluster) or "cluster"


def write_query_inputs(records: list[dict], outdir: Path) -> dict[str, int]:
    """按簇分文件落 outdir/<簇>.jsonl,返回 {簇: 条数}。"""
    groups: dict[str, list[dict]] = {}
    for p in build_query_inputs(records):
        groups.setdefault(_cluster_of(p["id"]), []).append(p)
    outdir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for cluster in sorted(groups):
        f = outdir / f"{_safe_cluster_filename(cluster)}.jsonl"
        f.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in groups[cluster]) + "\n",
            encoding="utf-8",
        )
        counts[cluster] = len(groups[cluster])
    return counts


# ---- ingest:写碎片 + 铺时间戳 + rebuild + 改写夹具 ----


def _spread_timestamps(anchor: date, n: int) -> list[str]:
    """把 n 条记录的时间戳均匀铺到 anchor(00:00 UTC)往前 365 天,首条最旧。

    第 i 条(1 起)= anchor - 365天 * (n-i+1)/n:首条恰好 anchor-365 天,
    末条 anchor 前 365/n 天。确定性:同输入同 anchor 必得同结果(裁定 3)。
    """
    anchor_dt = datetime(anchor.year, anchor.month, anchor.day, tzinfo=timezone.utc)
    out = []
    for i in range(1, n + 1):
        back = round(_DAYS_SPAN * 86400 * (n - i + 1) / n)
        out.append((anchor_dt - timedelta(seconds=back)).isoformat())
    return out


def run_ingest(
    cfg,
    corpus_path: Path,
    queries_path: Path,
    anchor: date,
    queries_out: Path,
    manifest_out: Path,
) -> int:
    """校验→写碎片→rebuild→改写夹具。中途失败不回滚(重跑 reset 即可,评测库可弃)。"""
    from memory_system import index
    from memory_system.embedding import get_provider
    from memory_system.fragments import Episode, Node, load_all_nodes, write_episode, write_node

    # ① 先跑 validate,有错即止,不写任何文件
    records, errs, warns = validate_corpus(corpus_path)
    corpus_ids = {r["id"] for r in records if isinstance(r.get("id"), str)}
    qrecords, qerrs, qwarns = validate_queries(queries_path, corpus_ids)
    _report_issues(errs + qerrs, warns + qwarns)
    if errs or qerrs:
        print(f"校验未通过({len(errs) + len(qerrs)} 个错误),已中止,未写任何文件。")
        return 1

    existing_eps = sorted(cfg.episodes_dir.glob("*.md")) if cfg.episodes_dir.exists() else []
    if existing_eps:
        print(f"[警告] 库内已有 {len(existing_eps)} 条 episode 碎片;评测建议先 `reset` 清库再 ingest。")

    # ② 临时 id → ep_syn+4 位行序号;时间戳按行序均匀铺开(首条最旧)
    n = len(records)
    stamps = _spread_timestamps(anchor, n)
    id_map: dict[str, str] = {}
    for i, (obj, ts) in enumerate(zip(records, stamps), start=1):
        pid = f"ep_syn{i:04d}"
        id_map[obj["id"]] = pid
        labels = list(dict.fromkeys(nd["label"] for nd in obj["nodes"]))  # 去重保序
        ep = Episode(
            public_id=pid,
            overview=obj["overview"],
            summary=obj["summary"],
            source_text=obj["source_text"],
            salience_tier=obj["salience_tier"],
            status="active",
            created_at=ts,
            activated_at=ts,
            highlights=[{"text": h["text"], "tag": h.get("tag")} for h in obj["highlights"]],
            keywords=[],
            nodes=labels,
        )
        write_episode(cfg.episodes_dir, ep)

    # ③ node:label 已存在(读 nodes 目录判断)则并入新别名重写;否则新建。
    #    别名全库唯一(node_aliases.alias 是 PRIMARY KEY),撞已有归属就跳过并告警,
    #    否则 rebuild 的 INSERT 会炸。
    now = _now_iso()
    known: dict[str, Node] = {nd.label: nd for _p, nd in load_all_nodes(cfg.nodes_dir)}
    alias_owner: dict[str, str] = {a: nd.label for nd in known.values() for a in nd.aliases}
    all_labels = set(known)
    for obj in records:
        for spec in obj["nodes"]:
            all_labels.add(spec["label"])
    created: set[str] = set()
    dirty: set[str] = set()
    merged = 0
    for obj in records:
        for spec in obj["nodes"]:
            label = spec["label"]
            if label not in known:
                known[label] = Node(label=label, type=spec.get("type"),
                                    created_at=now, updated_at=now)
                created.add(label)
                dirty.add(label)
            node = known[label]
            for a in spec.get("aliases") or []:
                if a == label or a in node.aliases:
                    continue
                owner = alias_owner.get(a)
                if owner is not None and owner != label:
                    print(f"[警告] 别名 {a!r} 已属于 node {owner!r},跳过(别名全库唯一)")
                    continue
                if a in all_labels:
                    print(f"[警告] 别名 {a!r} 与某个 node 的 label 同名,跳过(避免概念检索歧义)")
                    continue
                node.aliases.append(a)
                alias_owner[a] = label
                dirty.add(label)
                merged += 1
    for label in sorted(dirty):
        nd = known[label]
        if label not in created:
            nd.updated_at = now  # 老 node 并入了新别名 → 刷 updated_at
        write_node(cfg.nodes_dir, nd)
    print(f"碎片已写:episodes {n} 条;node 新建 {len(created)} 个、并入别名 {merged} 个。")

    # ④ 全量重建索引(真 embedding 会联网、耗额度;fake 离线)
    provider = get_provider(cfg.embedding)
    if cfg.embedding.provider != "fake":
        print(f"index.rebuild:即将联网嵌入全库 overview({provider.model},"
              f"{cfg.embedding.provider}),耗 embedding 额度……")
    rep = index.rebuild(cfg, provider)
    print(f"rebuild 完成:episodes={rep.episodes} nodes={rep.nodes} aliases={rep.aliases} "
          f"membrane={rep.membrane} vectors={rep.vectors}"
          + (f";桩 node: {rep.stub_nodes}" if rep.stub_nodes else ""))

    # ⑤ 改写 expect(临时 id → 真 public_id)落正式夹具 + manifest
    out_lines = [f"# eval_ingest 生成:{corpus_path.name} + {queries_path.name},"
                 f"anchor={anchor.isoformat()},共 {len(qrecords)} 题"]
    for q in qrecords:
        row: dict = {"query": q["query"], "mode": q["mode"]}
        if isinstance(q.get("context"), str):
            row["context"] = q["context"]
        row["expect"] = [id_map[t] for t in q["expect"]]
        if isinstance(q.get("type"), str):
            row["type"] = q["type"]
        if isinstance(q.get("note"), str):
            row["note"] = q["note"]
        out_lines.append(json.dumps(row, ensure_ascii=False))
    queries_out.parent.mkdir(parents=True, exist_ok=True)
    queries_out.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    manifest = {
        "generated_at": _now_iso(),
        "anchor": anchor.isoformat(),
        "days_span": _DAYS_SPAN,
        "n_episodes": n,
        "n_queries": len(qrecords),
        "embedding": {"model": provider.model, "dim": provider.dim},
        "id_map": id_map,
    }
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    print(f"夹具已落:{queries_out}({len(qrecords)} 题);manifest:{manifest_out}")
    return 0


# ---- reset:备份移动 + 重建空库(绝不删除文件)----


def _dir_files(d: Path) -> list[Path]:
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file())


def run_reset(cfg, *, yes: bool) -> int:
    """episodes/nodes 全量移到 <home>/backup_<UTC时间戳>/ 再 rebuild 成空库。

    只移动不删除;空库 rebuild 不调 embedding(零条 overview),无 key 也能跑。
    """
    from memory_system import index
    from memory_system.embedding import get_provider

    eps = _dir_files(cfg.episodes_dir)
    nds = _dir_files(cfg.nodes_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = cfg.home / f"backup_{stamp}"
    print(f"将移动 episodes {len(eps)} 个文件、nodes {len(nds)} 个文件 → {backup}/,"
          "随后重建空索引。不删除任何文件。")
    if not yes:
        ans = input("确认执行?[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消,未做任何改动。")
            return 1
    moved = 0
    for sub, files in (("episodes", eps), ("nodes", nds)):
        if not files:
            continue
        dst = backup / sub
        dst.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.move(str(f), str(dst / f.name))
            moved += 1
    rep = index.rebuild(cfg, get_provider(cfg.embedding))
    where = str(backup) if moved else "(碎片目录本就为空,未建备份目录)"
    print(f"reset 完成:移动 {moved} 个文件 → {where};rebuild 后库内 episodes={rep.episodes}。")
    return 0


# ---- 子命令入口 ----


def cmd_validate(args) -> int:
    records, errs, warns = validate_corpus(args.corpus)
    qn = 0
    if args.queries is not None:
        corpus_ids = {r["id"] for r in records if isinstance(r.get("id"), str)}
        qrecords, qerrs, qwarns = validate_queries(args.queries, corpus_ids)
        errs += qerrs
        warns += qwarns
        qn = len(qrecords)
    _report_issues(errs, warns)
    if errs:
        print(f"校验未通过:{len(errs)} 个错误、{len(warns)} 个警告。")
        return 1
    msg = f"校验通过:语料 {len(records)} 条"
    if args.queries is not None:
        msg += f",query {qn} 条"
    if warns:
        msg += f";警告 {len(warns)} 个(不阻断)"
    print(msg)
    return 0


def cmd_prep_queries(args) -> int:
    records, errs, warns = validate_corpus(args.corpus)
    _report_issues(errs, warns)
    if errs:
        print(f"校验未通过({len(errs)} 个错误),已中止。")
        return 1
    if args.by_cluster:
        counts = write_query_inputs(records, args.outdir)
        for cluster, cnt in counts.items():
            print(f"{args.outdir / (_safe_cluster_filename(cluster) + '.jsonl')}: {cnt} 条")
        print(f"共 {len(counts)} 个簇、{len(records)} 条出题输入(只含 id/source_text/nodes)。")
    else:
        for p in build_query_inputs(records):
            print(json.dumps(p, ensure_ascii=False))
    return 0


def _parse_anchor(raw: str | None) -> date:
    if raw is None:
        return datetime.now(timezone.utc).date()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"--anchor 需 YYYY-MM-DD 格式: {raw!r}")


def cmd_ingest(args) -> int:
    from memory_system.config import load_config

    cfg = load_config()
    try:
        return run_ingest(cfg, args.corpus, args.queries, _parse_anchor(args.anchor),
                          _DEFAULT_QUERIES_OUT, _DEFAULT_MANIFEST_OUT)
    except Exception as e:  # noqa: BLE001 —— 半途失败不回滚,指路 reset
        print(f"[失败] ingest 中途失败: {e!r}")
        print("已写入的碎片留在库里;先 `scripts/eval_ingest.py reset` 清库再重跑。")
        return 1


def cmd_reset(args) -> int:
    from memory_system.config import load_config

    return run_reset(load_config(), yes=args.yes)


# ---- 离线 fake 自测(不碰真实库)----


def run_selftest() -> int:
    """临时 MEMORY_SYSTEM_HOME + fake embedding,全程离线。环境变量先于 import config。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="memsys_evalingest_"))
    os.environ["MEMORY_SYSTEM_HOME"] = str(tmp)
    os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
    os.environ["MEMORY_EMBED_DIM"] = "16"
    os.environ["MEMORY_AGENT_PROVIDER"] = "fake"

    from memory_system.config import load_config
    from memory_system.db.connection import connect
    from memory_system.fragments import load_all_episodes, load_all_nodes

    cfg = load_config()
    for d in cfg.all_dirs():
        d.mkdir(parents=True, exist_ok=True)
    fixture = tmp / "fixture"
    fixture.mkdir()
    print(f"[selftest] 临时库: {tmp}")

    def _jsonl(path: Path, rows: list[dict]) -> Path:
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                        encoding="utf-8")
        return path

    # ---- 1) validate 抓坏行:一次报全(id 重复 / highlights 非子串 / tier 越界 / 坏 expect)----
    good_rows = [
        {"id": "syn-01",
         "source_text": "[我] 今天把合成检索的评测夹具跑通了,hit@1 有 0.8。\n[Claude] 不错,记得把阈值写进文档。",
         "overview": "合成检索评测夹具首跑,hit@1 达到 0.8,阈值待记录。",
         "summary": "从跑通夹具谈起,报出首轮指标,核心是 hit@1 0.8,落在把阈值写进文档。",
         "highlights": [{"text": "hit@1 有 0.8", "tag": "事实"}],
         "nodes": [{"label": "合成检索", "type": "project", "aliases": ["SynSearch"]},
                   {"label": "评测夹具", "type": "concept", "aliases": []}],
         "salience_tier": 2},
        {"id": "syn-02",
         "source_text": "[我] 合成检索的量化 trigram 盲区怎么办?\n[Claude] 两字中文词确实难,先记录再说。",
         "overview": "合成检索项目讨论量化 trigram 对两字中文词的盲区,决定先记录现象。",
         "summary": "从盲区问题开始,确认两字词是弱项,核心是先记录不急着修,落在观察数据。",
         "highlights": [],
         "nodes": [{"label": "合成检索", "type": "project", "aliases": ["合成搜索"]}],
         "salience_tier": 3},
        {"id": "oth-01",
         "source_text": "[我] 周末试了低温发酵面团,冷藏 18 小时。\n[Claude] 风味会更好,注意回温。",
         "overview": "低温发酵面团实验:冷藏 18 小时提升风味,出炉前需回温。",
         "summary": "从周末烘焙话头开始,谈低温发酵参数,核心是 18 小时冷藏,落在下次注意回温。",
         "highlights": [{"text": "冷藏 18 小时", "tag": "事实"}],
         "nodes": [{"label": "对照簇", "type": "concept"}],
         "salience_tier": 1},
        {"id": "oth-02",
         "source_text": "[我] 面团第二次实验,改冷藏 24 小时,有点过酸。\n[Claude] 那就折中 20 小时试试。",
         "overview": "面团低温发酵第二次实验:24 小时过酸,折中方案定为 20 小时。",
         "summary": "从二次实验结果说起,发现 24 小时过酸,核心是折中 20 小时,落在下轮验证。",
         "highlights": [],
         "nodes": [{"label": "对照簇", "type": "concept", "aliases": []}],
         "salience_tier": 2},
    ]
    bad_rows = [
        good_rows[0],
        {**good_rows[1], "id": "syn-01"},                                  # id 重复
        {**good_rows[2], "id": "oth-09",
         "highlights": [{"text": "这句话不在原文里", "tag": "金句"}]},        # 非逐字子串
        {**good_rows[3], "id": "oth-10", "salience_tier": 5},               # tier 越界
    ]
    bad_corpus = _jsonl(fixture / "bad_corpus.jsonl", bad_rows)
    records, errs, warns = validate_corpus(bad_corpus)
    assert len(records) == 4, records
    assert any("重复" in e for e in errs), errs
    assert any("逐字子串" in e for e in errs), errs
    assert any("salience_tier" in e for e in errs), errs
    assert len(errs) >= 3, ("应一次报全所有错,不 fail-fast", errs)

    bad_queries = _jsonl(fixture / "bad_queries.jsonl", [
        {"query": "不存在的引用", "mode": "episode", "expect": ["syn-99"], "type": "semantic"},
        {"query": "坏模式", "mode": "banana", "expect": ["syn-01"], "type": "semantic"},
        {"query": "负例带 expect", "mode": "episode", "expect": ["syn-01"], "type": "negative"},
    ])
    _, qerrs, _ = validate_queries(bad_queries, {r["id"] for r in good_rows})
    assert any("不存在的临时 id" in e for e in qerrs), qerrs
    assert any("mode" in e for e in qerrs), qerrs
    assert any("negative" in e for e in qerrs), qerrs
    print(f"[selftest] validate:坏语料报 {len(errs)} 错、坏 query 报 {len(qerrs)} 错,均一次报全。")

    # ---- 2) prep-queries:载荷只含 id/source_text/nodes,绝无 overview/summary ----
    corpus = _jsonl(fixture / "corpus.jsonl", good_rows)
    records, errs, warns = validate_corpus(corpus)
    assert not errs, errs
    payloads = build_query_inputs(records)
    assert len(payloads) == 4
    for p in payloads:
        assert set(p) == {"id", "source_text", "nodes"}, p
        assert all(isinstance(x, str) for x in p["nodes"]), p
        dumped = json.dumps(p, ensure_ascii=False)
        assert "overview" not in dumped and "summary" not in dumped, dumped
    counts = write_query_inputs(records, fixture / "query_inputs")
    assert counts == {"syn": 2, "oth": 2}, counts
    assert sorted(f.name for f in (fixture / "query_inputs").iterdir()) == ["oth.jsonl", "syn.jsonl"]
    print("[selftest] prep-queries:载荷无 overview/summary 泄漏;--by-cluster 分簇正确。")

    # ---- 3) ingest:条数 / 时间戳铺开 / node 并别名 / expect 改写 / manifest ----
    queries_raw = _jsonl(fixture / "queries_raw.jsonl", [
        {"query": "那个检索指标第一次跑出来多少来着", "mode": "episode",
         "expect": ["syn-01"], "type": "semantic", "note": "语义改写"},
        {"query": "冷藏 18 小时", "mode": "detail",
         "expect": ["oth-01"], "type": "verbatim", "note": "原文逐字"},
        {"query": "合成检索", "mode": "concept",
         "expect": ["syn-01", "syn-02"], "type": "concept", "note": "挂该 node 的全部记忆"},
        {"query": "有没有聊过健身计划", "mode": "episode",
         "expect": [], "type": "negative", "note": "库中无此内容"},
    ])
    anchor = date(2026, 7, 1)
    q_out = tmp / "eval_out" / "queries.jsonl"
    m_out = tmp / "eval_out" / "manifest.json"
    rc = run_ingest(cfg, corpus, queries_raw, anchor, q_out, m_out)
    assert rc == 0, rc

    eps = {ep.public_id: ep for _p, ep in load_all_episodes(cfg.episodes_dir)}
    assert set(eps) == {"ep_syn0001", "ep_syn0002", "ep_syn0003", "ep_syn0004"}, sorted(eps)
    # 时间戳:首条最旧 = anchor-365 天;首末不同且严格递增;created_at == activated_at
    assert eps["ep_syn0001"].activated_at == "2025-07-01T00:00:00+00:00", eps["ep_syn0001"]
    stamps = [eps[f"ep_syn{i:04d}"].activated_at for i in range(1, 5)]
    assert stamps[0] != stamps[-1] and stamps == sorted(stamps) and len(set(stamps)) == 4, stamps
    assert all(ep.created_at == ep.activated_at for ep in eps.values())
    assert all(ep.status == "active" for ep in eps.values())
    # node:同 label 两条语料的别名并进同一个碎片,不重复建
    nodes = {nd.label: nd for _p, nd in load_all_nodes(cfg.nodes_dir)}
    assert set(nodes) == {"合成检索", "评测夹具", "对照簇"}, sorted(nodes)
    assert set(nodes["合成检索"].aliases) == {"SynSearch", "合成搜索"}, nodes["合成检索"]
    # DB:rebuild 后条数一致
    con = connect(cfg.db_path)
    try:
        n_db = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        n_vec = con.execute("SELECT COUNT(*) FROM episode_vectors").fetchone()[0]
    finally:
        con.close()
    assert n_db == 4 and n_vec == 4, (n_db, n_vec)
    # 夹具:expect 全部改写成 ep_syn 形式;type/note 保留;负例空 expect
    qlines = [json.loads(s) for _ln, s in _iter_jsonl(q_out)]
    assert len(qlines) == 4, qlines
    for q in qlines:
        assert all(re.fullmatch(r"ep_syn\d{4}", e) for e in q["expect"]), q
        assert "type" in q and "note" in q, q
    assert qlines[2]["expect"] == ["ep_syn0001", "ep_syn0002"], qlines[2]
    assert qlines[3]["expect"] == [] and qlines[3]["type"] == "negative", qlines[3]
    manifest = json.loads(m_out.read_text(encoding="utf-8"))
    assert manifest["id_map"]["syn-01"] == "ep_syn0001", manifest
    assert manifest["n_episodes"] == 4 and manifest["n_queries"] == 4, manifest
    assert manifest["embedding"] == {"model": "fake", "dim": 16}, manifest
    assert manifest["anchor"] == "2026-07-01", manifest
    print("[selftest] ingest:4 条落库、时间戳铺开(首=anchor-365d)、别名并入、expect 已改写。")

    # ---- 4) reset:备份移动、库空、备份文件数对得上 ----
    n_files_before = len(_dir_files(cfg.episodes_dir)) + len(_dir_files(cfg.nodes_dir))
    assert n_files_before == 7, n_files_before  # 4 episode + 3 node
    rc = run_reset(cfg, yes=True)
    assert rc == 0, rc
    assert _dir_files(cfg.episodes_dir) == [] and _dir_files(cfg.nodes_dir) == []
    backups = sorted(cfg.home.glob("backup_*"))
    assert len(backups) == 1, backups
    n_backed = len(_dir_files(backups[0] / "episodes")) + len(_dir_files(backups[0] / "nodes"))
    assert n_backed == n_files_before, (n_backed, n_files_before)
    con = connect(cfg.db_path)
    try:
        n_db = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    finally:
        con.close()
    assert n_db == 0, n_db
    print(f"[selftest] reset:{n_backed} 个文件全数移入 {backups[0].name}/,库已空。")

    print("[selftest] PASS: validate 一次报全 / prep 无泄漏 / ingest 落库正确 / reset 只移不删。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="召回评测语料落库工具链:validate / prep-queries / ingest / reset。")
    ap.add_argument("--selftest", action="store_true", help="离线 fake 自测,不碰真实库")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("validate", help="只校验不落盘,报错带行号、一次报全")
    p.add_argument("corpus", type=Path, help="语料 corpus.jsonl")
    p.add_argument("queries", type=Path, nargs="?", default=None, help="题目 queries_raw.jsonl(可选)")

    p = sub.add_parser("prep-queries", help="输出出题载荷(只含 id/source_text/nodes)")
    p.add_argument("corpus", type=Path, help="语料 corpus.jsonl")
    p.add_argument("--by-cluster", action="store_true", help="按 id 前缀分文件落 outdir")
    p.add_argument("--outdir", type=Path, default=_DEFAULT_QUERY_INPUTS,
                   help=f"--by-cluster 的落点(默认 {_DEFAULT_QUERY_INPUTS})")

    p = sub.add_parser("ingest", help="校验→写碎片→rebuild→改写夹具(真 embedding 联网)")
    p.add_argument("corpus", type=Path, help="语料 corpus.jsonl")
    p.add_argument("queries", type=Path, help="题目 queries_raw.jsonl")
    p.add_argument("--anchor", default=None, help="时间戳锚点 YYYY-MM-DD(默认今天 UTC)")

    p = sub.add_parser("reset", help="episodes/nodes 移入备份目录再重建空库(绝不删除)")
    p.add_argument("--yes", action="store_true", help="跳过交互确认")

    args = ap.parse_args(argv)
    if args.selftest:
        return run_selftest()
    if args.cmd == "validate":
        return cmd_validate(args)
    if args.cmd == "prep-queries":
        return cmd_prep_queries(args)
    if args.cmd == "ingest":
        return cmd_ingest(args)
    if args.cmd == "reset":
        return cmd_reset(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
