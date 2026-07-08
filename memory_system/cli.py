"""命令行入口:init / migrate / doctor / diagnose / embed。"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from memory_system import __version__
from memory_system.config import Config, load_config
from memory_system.db import migrate
from memory_system.db.connection import connect, vec_version
from memory_system.log import setup_logging


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


_ENV_EXAMPLE = """\
# memory_system 配置 —— 此文件在数据主目录内、在仓库之外。
# 把本文件复制为同目录下的 .env 并填入真实 key;.env 会被所有 memory-system 命令自动加载。
# 已经 export 到环境的变量优先于 .env(真实 export 压过此处)。

DASHSCOPE_API_KEY=

# 可选覆盖(一般不用动):
# MEMORY_EMBED_BASE_URL=https://ws-0rc5n2o7rajktheg.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
# MEMORY_EMBED_MODEL=text-embedding-v4
# MEMORY_EMBED_DIM=1024
# MEMORY_EMBED_KEY_ENV=DASHSCOPE_API_KEY
"""


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    log = setup_logging(cfg.logs_dir)
    for d in cfg.all_dirs():
        d.mkdir(parents=True, exist_ok=True)
    # 放一份 .env 模板(不覆盖已存在的 .env;example 可刷新)
    (cfg.home / ".env.example").write_text(_ENV_EXAMPLE, encoding="utf-8")
    log.info("主目录就绪: %s", cfg.home)
    if not (cfg.home / ".env").exists():
        log.info("把 %s 复制为 .env 并填入 key", cfg.home / ".env.example")

    con = connect(cfg.db_path)
    try:
        applied = migrate.up(con)
        if applied:
            log.info("应用迁移: %s", applied)
        # 写/校验 embedding 锁。fake 不写锁(只是占位 provider)。
        if cfg.embedding.provider != "fake":
            existing = dict(con.execute("SELECT key, value FROM meta").fetchall())
            want = {"embedding_model": cfg.embedding.model, "embedding_dim": str(cfg.embedding.dim)}
            for k, v in want.items():
                if k in existing and existing[k] != v:
                    log.error("meta 冲突: %s 已是 %r,config 想写 %r。换模型需全量重嵌。",
                              k, existing[k], v)
                    return 2
            for k, v in want.items():
                _set_meta(con, k, v)
            _set_meta(con, "schema_version", str(migrate.current_version(con)))
            con.commit()
        log.info("vec_version=%s  schema_version=%s", vec_version(con), migrate.current_version(con))
    finally:
        con.close()
    print(f"init 完成: {cfg.home}")
    return 0


def cmd_migrate(cfg: Config, args: argparse.Namespace) -> int:
    con = connect(cfg.db_path)
    try:
        if args.action == "status":
            cur = migrate.current_version(con)
            print(f"当前版本: {cur}")
            for v, name, applied in migrate.status(con):
                print(f"  [{'x' if applied else ' '}] {v:03d} {name}")
        elif args.action == "up":
            applied = migrate.up(con, target=args.target)
            print(f"应用: {applied or '（无新迁移）'}")
        elif args.action == "down":
            rolled = migrate.down(con, steps=args.steps)
            print(f"回滚: {rolled or '（无可回滚）'}")
    finally:
        con.close()
    return 0


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    problems = 0
    print(f"home: {cfg.home}  ({'存在' if cfg.home.exists() else '缺失'})")
    for d in cfg.all_dirs():
        ok = d.exists()
        problems += 0 if ok else 1
        print(f"  [{'x' if ok else ' '}] {d}")

    # embedding key 状态(不打印 key 本身,只报有无 + 来源)
    env_file = cfg.home / ".env"
    key_name = cfg.embedding.api_key_env
    key_val = os.environ.get(key_name)
    masked = f"{key_val[:6]}…{key_val[-4:]}" if key_val and len(key_val) > 12 else ("已设置" if key_val else "")
    print(f".env: {'存在' if env_file.exists() else '无'}  ({env_file})")
    print(f"{key_name}: {masked or '未设置 —— 真 embedding 会 401'}")
    try:
        con = connect(cfg.db_path)
        try:
            print(f"sqlite-vec: {vec_version(con)}")
            print(f"schema 版本: {migrate.current_version(con)}")
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall()) if \
                con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
                ).fetchone() else {}
            print(f"meta: {meta or '（空,未 init?）'}")
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        problems += 1
        print(f"  DB 检查失败: {e}")
    # 碎片 ↔ DB 一致性(铁律 1:碎片是正本,DB 是可重建索引)。两类漂移:
    #   ① 碎片有、DB 无 → 索引落后,`index rebuild` 可无损补回(可修)
    #   ② DB 有、碎片缺 → 悬空索引行,正本已失(真损坏,rebuild 救不回)
    try:
        from memory_system.fragments import read_episode, read_node

        def _scan(d: Path, ident):
            ids: dict[str, Path] = {}
            bad: list[tuple[str, Exception]] = []
            for p in sorted(d.glob("*.md")):
                try:
                    ids[ident(p)] = p
                except Exception as e:  # noqa: BLE001  坏碎片不该让整轮体检崩
                    bad.append((p.name, e))
            return ids, bad

        disk_eps, bad_eps = _scan(cfg.episodes_dir, lambda p: read_episode(p).public_id)
        disk_nodes, bad_nodes = _scan(cfg.nodes_dir, lambda p: read_node(p).label)
        con = connect(cfg.db_path)
        try:
            db_eps = {pid for (pid,) in con.execute("SELECT public_id FROM episodes")}
            db_nodes = {lab for (lab,) in con.execute("SELECT label FROM nodes")}
        finally:
            con.close()

        frag_only_ep, frag_only_nd = set(disk_eps) - db_eps, set(disk_nodes) - db_nodes
        db_only_ep, db_only_nd = db_eps - set(disk_eps), db_nodes - set(disk_nodes)

        for name, err in bad_eps + bad_nodes:
            problems += 1
            print(f"  坏碎片(无法解析): {name} —— {err}")
        if frag_only_ep or frag_only_nd:
            problems += 1
            print(f"  碎片有、DB 无(索引落后,跑 `index rebuild` 补回): "
                  f"episode×{len(frag_only_ep)} node×{len(frag_only_nd)}")
            for pid in sorted(frag_only_ep):
                print(f"      ep  {pid}")
            for lab in sorted(frag_only_nd):
                print(f"      nd  {lab}")
        if db_only_ep or db_only_nd:
            problems += 1
            print(f"  DB 有、碎片缺(悬空索引,正本已失!): "
                  f"episode×{len(db_only_ep)} node×{len(db_only_nd)}")
            for pid in sorted(db_only_ep):
                print(f"      ep  {pid}")
            for lab in sorted(db_only_nd):
                print(f"      nd  {lab}")
        if not (bad_eps or bad_nodes or frag_only_ep or frag_only_nd or db_only_ep or db_only_nd):
            print(f"碎片↔DB 一致性: OK(episode×{len(disk_eps)} node×{len(disk_nodes)} 对齐)")
    except Exception as e:  # noqa: BLE001
        problems += 1
        print(f"  一致性检查失败: {e}")
    print("OK" if problems == 0 else f"发现 {problems} 个问题")
    return 0 if problems == 0 else 1


def cmd_diagnose(cfg: Config, args: argparse.Namespace) -> int:
    from memory_system.diagnose import diagnose_claude_code

    if args.target == "claude-code":
        out = diagnose_claude_code(cfg)
        print(f"诊断报告: {out}")
        return 0
    print(f"未知诊断目标: {args.target}")
    return 1


def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    from memory_system.transcript import discover

    infos = discover(cfg.transcripts_root, count_lines=True)  # 诊断命令,显式要行数
    if not infos:
        print(f"未发现 transcript(根目录: {cfg.transcripts_root})")
        return 0
    print(f"发现 {len(infos)} 个 transcript(根目录: {cfg.transcripts_root}):")
    for i in infos[: args.limit]:
        flag = " ⚠正在写入" if i.maybe_writing else ""
        print(f"  {i.session_id[:8]}  行={i.line_count:<5} {i.size // 1024:>5}KB  {i.cwd}{flag}")
    if len(infos) > args.limit:
        print(f"  …还有 {len(infos) - args.limit} 个(--limit 调整)")
    return 0


def cmd_preview(cfg: Config, args: argparse.Namespace) -> int:
    from pathlib import Path

    from memory_system import preview_cache
    from memory_system.preprocess import render

    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1
    ct = preview_cache.get(cfg.preview_cache_dir, path)
    text, _lmap = render(ct)
    if args.turns:
        print(f"# {ct.session_id}  {len(ct.turns)} 回合  跳过 sidechain {ct.skipped_sidechain}")
        for t in ct.turns:
            print(f"\n--- 回合 {t.idx}  ({len(t.uuids)} msg) ---")
            if t.human_text:
                print(f"[我]: {t.human_text[:200]}")
            if t.assistant_text:
                print(f"[Claude]: {t.assistant_text[:200]}")
    else:
        print(text)
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from memory_system.server import serve

    serve(cfg, host=args.host, port=args.port)
    return 0


def cmd_chunk(cfg: Config, args: argparse.Namespace) -> int:
    from dataclasses import replace
    from pathlib import Path

    from memory_system import preview_cache, segments_store
    from memory_system.agent import get_chat_provider
    from memory_system.chunk import (
        ChunkFailed,
        OversizedError,
        manual_segments,
        run_chunk,
        validate_segments,
    )

    def _check_segments(segs: list[dict]) -> bool:
        """P1-B:重叠报错(返回 False),空洞打印警告。"""
        vr = validate_segments(segs, {t.idx for t in ct.turns})
        for g in vr["gaps"]:
            print(f"  ⚠ 空洞:回合 {g[0]}-{g[1]} 未被任何段覆盖(不入库)")
        if not vr["ok"]:
            for o in vr["overlaps"]:
                print(f"  ✗ 重叠:段 {o['a']} 与 {o['b']} 在回合 {o['range'][0]}-{o['range'][1]}")
            print("段重叠会重复入库,已拒绝保存;请先消除重叠")
            return False
        return True

    log = setup_logging(cfg.logs_dir)
    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1
    ct = preview_cache.get(cfg.preview_cache_dir, path)
    if not ct.turns:
        print("清洗后 0 回合(空壳),无可切内容")
        return 1
    mtime = path.stat().st_mtime

    if args.manual:
        try:
            bounds = []
            for part in args.manual.split(","):
                a, b = part.split("-")
                bounds.append((int(a), int(b)))
        except ValueError:
            print("--manual 形如 1-8,9-20(回合号)")
            return 1
        segs = manual_segments(ct, bounds)
        if not _check_segments(segs):
            return 1
        doc = segments_store.save_full(cfg.chunks_dir, ct.session_id, str(path), mtime, segs)
        print(f"手动切块 {len(segs)} 段,落: {segments_store.path_for(cfg.chunks_dir, ct.session_id)}")
        _print_segments(doc["segments"])
        return 0

    agent_cfg = replace(cfg.agent, provider=cfg.agent.provider_for("chunk"))
    if args.provider:
        agent_cfg = replace(agent_cfg, provider=args.provider)
    model = args.model or agent_cfg.chunk_model
    provider = get_chat_provider(agent_cfg)
    ok, why = provider.available()
    if not ok:
        print(f"provider {agent_cfg.provider} 不可用: {why}")
        return 1
    try:
        res = run_chunk(ct, provider, model=model, timeout=agent_cfg.timeout_s,
                        max_retries=agent_cfg.max_retries)
    except OversizedError as e:
        print(f"超大: {e}")
        return 2
    except ChunkFailed as e:
        segments_store.append_retry(cfg.chunks_dir, ct.session_id, str(path), mtime,
                                    provider=agent_cfg.provider, model=model, error=str(e))
        log.error("切块失败: %s", e)
        print(f"切块失败(已记入 retry 列表): {e}")
        return 1
    if not _check_segments(res.segments):
        return 1
    doc = segments_store.record_agent_run(cfg.chunks_dir, ct.session_id, str(path), mtime, res)
    print(f"切块完成: {len(res.segments)} 段  provider={res.provider} model={res.model} "
          f"尝试={res.attempts} cost={res.cost_usd}")
    _print_segments(doc["segments"])
    return 0


def _print_segments(segments: list[dict]) -> None:
    for s in segments:
        short = " [short]" if s.get("short") else ""
        dels = f"  删{len(s.get('deletions') or [])}" if s.get("deletions") else ""
        print(f"  {s['seg_id']}: 回合 {s['start_turn']}-{s['end_turn']}{short}  "
              f"[{s.get('origin')}] {s.get('tag') or '(无 tag)'}{dels}")
        if s.get("cut_reason"):
            print(f"       ↳ {s['cut_reason']}")


def cmd_extract(cfg: Config, args: argparse.Namespace) -> int:
    from dataclasses import replace
    from pathlib import Path

    from memory_system import preview_cache, segments_store, staging_store
    from memory_system.agent import get_chat_provider
    from memory_system.extract import existing_nodes, extract_segments

    log = setup_logging(cfg.logs_dir)
    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1
    ct = preview_cache.get(cfg.preview_cache_dir, path)
    if not ct.turns:
        print("清洗后 0 回合(空壳),无可提取内容")
        return 1

    doc = segments_store.load(cfg.chunks_dir, ct.session_id)
    if not doc or not doc.get("segments"):
        print("无切块段;请先 `memory-system chunk` 切块并确认分段再提取")
        return 1
    segments = doc["segments"]
    if args.seg:
        want = {s.strip() for s in args.seg.split(",") if s.strip()}
        segments = [s for s in segments if s.get("seg_id") in want]
        if not segments:
            print(f"无匹配 seg_id: {sorted(want)}")
            return 1

    agent_cfg = replace(cfg.agent, provider=cfg.agent.provider_for("extract"))
    if args.provider:
        agent_cfg = replace(agent_cfg, provider=args.provider)
    model = args.model or agent_cfg.extract_model
    provider = get_chat_provider(agent_cfg)
    ok, why = provider.available()
    if not ok:
        print(f"provider {agent_cfg.provider} 不可用: {why}")
        return 1

    nodes = existing_nodes(cfg.nodes_dir)
    batch = extract_segments(ct, segments, provider, nodes, model=model,
                             timeout=agent_cfg.timeout_s, max_retries=agent_cfg.max_retries)
    sdir = cfg.staging_episodes_dir
    ts_by_turn = {t.idx: t.timestamp for t in ct.turns}
    for seg, res, src in batch.staged:
        staging_store.upsert_episode(sdir, ct.session_id, str(path), seg, res, src,
                                     created_at=ts_by_turn.get(seg["start_turn"]))
    for seg, errors in batch.failed:
        staging_store.append_retry(sdir, ct.session_id, str(path), seg,
                                   provider=agent_cfg.provider, model=model, errors=errors)
        log.error("提取失败 seg=%s: %s", seg.get("seg_id"), errors)

    print(f"提取完成: {len(batch.staged)} 段进 staging,{len(batch.failed)} 段进 retry  "
          f"provider={provider.id} model={model}")
    sdoc = staging_store.load(sdir, ct.session_id)
    if sdoc:
        _print_staging(sdoc)
    return 0 if not batch.failed else 2


def _print_staging(doc: dict) -> None:
    for e in doc.get("episodes", []):
        nodes = e.get("nodes") or []
        hl = e.get("highlights") or []
        print(f"  {e.get('stage_id')}({e.get('seg_id')}): 回合 {e.get('start_turn')}-"
              f"{e.get('end_turn')}  tier={e.get('salience_tier')}  "
              f"node{len(nodes)} highlight{len(hl)}")
        ov = (e.get("overview") or "").replace("\n", " ")
        print(f"       ↳ {ov[:70]}")
    for r in doc.get("retry", []):
        print(f"  [retry] {r.get('seg_id')}: 回合 {r.get('start_turn')}-{r.get('end_turn')}  "
              f"{'; '.join(r.get('errors') or [])[:80]}")


def cmd_confirm(cfg: Config, args: argparse.Namespace) -> int:
    from dataclasses import replace
    from pathlib import Path

    from memory_system import archive, preview_cache, staging_store
    from memory_system.embedding import get_provider

    log = setup_logging(cfg.logs_dir)
    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1
    ct = preview_cache.get(cfg.preview_cache_dir, path)
    doc = staging_store.load(cfg.staging_episodes_dir, ct.session_id)
    if not doc or not doc.get("episodes"):
        print("无 staging episode 可确认;请先 extract")
        return 1
    emb_cfg = cfg.embedding if not args.provider else replace(cfg.embedding, provider=args.provider)
    provider = get_provider(emb_cfg)

    if args.all:
        stage_ids = [e["stage_id"] for e in doc["episodes"]]
    elif args.stage:
        stage_ids = [args.stage]
    else:
        print("指定 --stage e1 或 --all")
        return 1
    ok_n = 0
    for sid in stage_ids:
        try:
            pid = archive.confirm_episode(cfg, ct.session_id, sid, provider)
        except archive.ArchiveError as e:
            log.error("确认 %s 失败: %s", sid, e)
            print(f"  ✗ {sid}: {e}")
            return 2
        print(f"  ✓ {sid} → active  {pid}")
        ok_n += 1
    print(f"确认 {ok_n} 条成 active 碎片(已增量入库)")
    return 0


def cmd_reject(cfg: Config, args: argparse.Namespace) -> int:
    from pathlib import Path

    from memory_system import archive, preview_cache

    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1
    ct = preview_cache.get(cfg.preview_cache_dir, path)
    try:
        archive.reject_episode(cfg, ct.session_id, args.stage, args.reason)
    except archive.ArchiveError as e:
        print(f"拒绝失败: {e}")
        return 1
    print(f"已拒 {args.stage}(留痕 rejected,未入库)")
    return 0


def cmd_archive(cfg: Config, args: argparse.Namespace) -> int:
    from memory_system import archive

    try:
        archive.archive_episode(cfg, args.public_id)
    except archive.ArchiveError as e:
        print(f"归档失败: {e}")
        return 1
    print(f"已归档 {args.public_id}(active → archived,不再被检索注入)")
    return 0


def cmd_delete(cfg: Config, args: argparse.Namespace) -> int:
    from memory_system import archive

    try:
        if args.target == "episode":
            rep = archive.delete_episode(cfg, args.public_id)
            print(f"已删除 episode {rep.public_id}(碎片正本 + DB 索引/膜/向量/FTS,永久移除)")
            if rep.orphaned_nodes:
                print("  以下 node 因此变成孤儿(已保留;如确认无用可 `memory-system delete node <label>` 清理):")
                for lab in rep.orphaned_nodes:
                    print(f"      {lab}")
        else:  # node
            rep = archive.delete_node(cfg, args.label)
            print(f"已删除 node {rep.label}(碎片正本 + DB 节点/别名/膜,永久移除)")
            if rep.dereferenced_episodes:
                print(f"  已从 {len(rep.dereferenced_episodes)} 条 episode 碎片摘除该 node 引用:")
                for pid in rep.dereferenced_episodes:
                    print(f"      {pid}")
    except archive.ArchiveError as e:
        print(f"删除失败: {e}")
        return 1
    return 0


def cmd_recall(cfg: Config, args: argparse.Namespace) -> int:
    """检索(S6):二级动作 detail / episode / concept。"""
    if args.action == "detail":
        return _recall_detail(cfg, args)
    if args.action == "episode":
        return _recall_episode(cfg, args)
    if args.action == "concept":
        return _recall_concept(cfg, args)
    print(f"未知 recall 动作: {args.action}")
    return 1


def _recall_detail(cfg: Config, args: argparse.Namespace) -> int:
    import json

    from memory_system.recall import recall_detail

    result = recall_detail(cfg, args.query, since=args.since, until=args.until,
                           raw=args.raw, limit=args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    hits = result["hits"]
    if not hits:
        print(f"未命中「{args.query}」。")
        print("提示:库内原文没有逐字包含该词的段落(<3 字短词已自动走子串回退),"
              "换更具体的词或调整时间窗再试。")
        return 0
    print(f"细节检索「{args.query}」命中 {len(hits)} 条:")
    for h in hits:
        print(f"  {h['public_id']}  [{h.get('created_at') or ''}]  tier={h.get('salience_tier')}")
        print(f"       {h['window']}")
    return 0


def _print_episode_structured(result: dict) -> None:
    """episode 结构化槽位的人类可读渲染(--raw 与重构降级共用)。"""
    slots = result["slots"]
    print(f"情景检索「{result['query']}」  frame_nodes: {', '.join(result['frame_nodes']) or '(无)'}")
    print("主槽:")
    for h in slots["primary"]:
        print(f"  {h['public_id']}  [{h.get('created_at') or ''}]  "
              f"tier={h.get('salience_tier')}  score={h['score']}")
        ov = (h.get("overview") or "").replace("\n", " ")
        print(f"       ↳ {ov[:70]}")
    if slots["same_source"]:
        print("同源:")
        for h in slots["same_source"]:
            sm = (h.get("summary") or "").replace("\n", " ")
            print(f"  {h['public_id']}  [{h.get('created_at') or ''}]  {sm[:60]}")
    if slots["associative"]:
        print("联想:")
        for h in slots["associative"]:
            sm = (h.get("summary") or "").replace("\n", " ")
            print(f"  {h['public_id']}  (via {', '.join(h.get('via_nodes') or [])})  {sm[:60]}")


def _print_concept_structured(result: dict) -> None:
    """concept 结构化条目的人类可读渲染(--raw 与重构降级共用)。"""
    print(f"概念检索「{result['node']}」  挂载 {len(result['episodes'])} 条情景")
    if result["alias_bridge"]:
        print(f"  {result['alias_bridge']}")
    for e in result["episodes"]:
        print(f"  {e['public_id']}  [{e.get('created_at') or ''}]  "
              f"tier={e['salience_tier']}  activation={e['activation']}")
        sm = (e.get("summary") or "").replace("\n", " ")
        print(f"       ↳ {sm[:70]}")
        for hl in e.get("highlights") or []:
            print(f"       「{hl.get('text', '')}」")


def _recall_episode(cfg: Config, args: argparse.Namespace) -> int:
    import json

    from memory_system.agent.base import ChatError
    from memory_system.recall import recall_episode, reconstruct

    try:
        result = recall_episode(cfg, args.query, session_key=args.session)
    except ValueError as e:  # meta 锁不符:查询向量与库内向量不同模型/维度
        print(f"检索拒绝: {e}")
        return 2
    if args.json:  # 机器可读 = §5 结构化契约,不走重构
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not result["slots"]["primary"]:
        print(f"未命中「{args.query}」。")
        print("提示:库为空或候选全被过滤;FTS trigram 对少于 3 个字符的中文词不可靠,可换个说法再试。")
        return 0
    if args.raw:  # 调试逃生口:结构化槽位,不调 chat provider
        _print_episode_structured(result)
        return 0
    # 默认:重构成自然语言(S6-5)。候选集已定死并写日志,LLM 只做表达。
    setup_logging(cfg.logs_dir)
    try:
        text = reconstruct.run(cfg, "episode", result, args.query)
    except ChatError as e:
        print(f"重构失败(已降级为 --raw 结构化输出): {e}")
        _print_episode_structured(result)
        return 3  # 非零但不吞结果
    print(text)
    return 0


def _recall_concept(cfg: Config, args: argparse.Namespace) -> int:
    import json

    from memory_system.agent.base import ChatError
    from memory_system.recall import recall_concept, reconstruct
    from memory_system.recall.concept import NodeMissError

    try:
        result = recall_concept(cfg, args.node, context=args.context)
    except NodeMissError as e:
        print(f"没有叫「{e.query}」的概念(label / 别名都查过)。")
        if e.suggestions:
            print("  也许你想找:")
            for lab in e.suggestions:
                print(f"    {lab}")
        return 1
    except ValueError as e:  # meta 锁不符(--context 走了查询向量)
        print(f"检索拒绝: {e}")
        return 2
    if args.json:  # 机器可读 = §5 结构化契约,不走重构
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not result["episodes"]:
        print(f"概念「{result['node']}」下没有挂载中的 active 情景,无可重构。")
        return 0
    if args.raw:  # 调试逃生口:结构化条目,不调 chat provider
        _print_concept_structured(result)
        return 0
    # 默认:重构成综合立场(S6-5)。概念必重构(§0.1)。
    setup_logging(cfg.logs_dir)
    user_query = args.node if not args.context else f"{args.node}(语境: {args.context})"
    try:
        text = reconstruct.run(cfg, "concept", result, user_query)
    except ChatError as e:
        print(f"重构失败(已降级为 --raw 结构化输出): {e}")
        _print_concept_structured(result)
        return 3  # 非零但不吞结果
    print(text)
    return 0


def cmd_opening(cfg: Config, args: argparse.Namespace) -> int:
    """开场注入(S6-6):rebuild(重建缓存)/ show(展示缓存)。"""
    from memory_system.agent.base import ChatError
    from memory_system.recall import opening

    if args.action == "rebuild":
        setup_logging(cfg.logs_dir)  # reconstruct 要写候选集日志(召回可重放)
        try:
            text = opening.rebuild_opening(cfg, force=args.force)
        except ChatError as e:  # 重构失败:cache 不动、dirty 保留,下次重试
            print(f"开场重构失败(cache 未动,.dirty 保留待重试): {e}")
            return 3
        if text is None:  # 无 .dirty 且未 --force → 跳过
            print("开场缓存无需重建(无 .dirty 标记;--force 可强制重建)。")
            return 0
        print(f"开场缓存已重建: {opening.cache_path(cfg)}")
        return 0
    if args.action == "show":
        cache = opening.cache_path(cfg)
        if not cache.exists():
            print("开场缓存不存在;先跑 `memory-system opening rebuild --force` 生成。")
            return 1
        text = cache.read_text(encoding="utf-8")
        print(text, end="" if text.endswith("\n") else "\n")
        return 0
    print(f"未知 opening 动作: {args.action}")
    return 1


def cmd_index(cfg: Config, args: argparse.Namespace) -> int:
    from dataclasses import replace

    from memory_system.embedding import get_provider
    from memory_system.index import rebuild

    if args.action != "rebuild":
        print(f"未知 index 动作: {args.action}")
        return 1
    log = setup_logging(cfg.logs_dir)
    emb_cfg = cfg.embedding if not args.provider else replace(cfg.embedding, provider=args.provider)
    if emb_cfg.provider == "fake" and cfg.embedding.provider != "fake":
        print("拒绝在真实 embedding 配置下用 fake 重建索引;请使用临时 MEMORY_SYSTEM_HOME 跑离线测试。")
        return 1
    provider = get_provider(emb_cfg)
    try:
        rep = rebuild(cfg, provider, lock_meta=emb_cfg.provider != "fake")
    except Exception as e:  # noqa: BLE001
        log.error("rebuild 失败: %s", e)
        print(f"rebuild 失败: {e}")
        return 1
    print(
        f"rebuild 完成: nodes={rep.nodes} aliases={rep.aliases} episodes={rep.episodes} "
        f"膜={rep.membrane} 向量={rep.vectors}"
    )
    if rep.stub_nodes:
        print(f"  警告:膜引用了无碎片的 node,已建桩: {rep.stub_nodes}")
    return 0


def cmd_embed(cfg: Config, args: argparse.Namespace) -> int:
    from dataclasses import replace

    from memory_system.embedding import get_provider

    emb_cfg = cfg.embedding if not args.provider else replace(cfg.embedding, provider=args.provider)
    provider = get_provider(emb_cfg)
    try:
        vecs = provider.embed(args.text)
    except Exception as e:  # noqa: BLE001
        print(f"embedding 失败: {e}")
        return 1
    for t, v in zip(args.text, vecs):
        print(f"[{provider.model}] dim={len(v)} first5={[round(x, 4) for x in v[:5]]}  «{t[:24]}»")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memory-system", description="Claude Code 持久化记忆系统")
    p.add_argument("--version", action="version", version=f"memory_system {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建数据主目录并初始化(幂等)").set_defaults(func=cmd_init)

    mp = sub.add_parser("migrate", help="schema 迁移")
    msub = mp.add_subparsers(dest="action", required=True)
    msub.add_parser("status", help="查看迁移状态")
    up = msub.add_parser("up", help="应用迁移")
    up.add_argument("--target", type=int, default=None, help="只升到该版本")
    dn = msub.add_parser("down", help="回滚迁移")
    dn.add_argument("--steps", type=int, default=1, help="回滚步数")
    mp.set_defaults(func=cmd_migrate)

    sub.add_parser("doctor", help="健康检查").set_defaults(func=cmd_doctor)

    sp = sub.add_parser("scan", help="列出 transcript")
    sp.add_argument("--limit", type=int, default=30, help="最多列出几个")
    sp.set_defaults(func=cmd_scan)

    pp = sub.add_parser("preview", help="预览清洗后的对话")
    pp.add_argument("path", help="jsonl 路径")
    pp.add_argument("--turns", action="store_true", help="按回合分块显示(带 msg 计数)")
    pp.set_defaults(func=cmd_preview)

    sv = sub.add_parser("serve", help="启动本地审核前端(零依赖)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(func=cmd_serve)

    ck = sub.add_parser("chunk", help="切块(Prompt 1):调 agent 建议分段,落工作文件")
    ck.add_argument("path", help="jsonl 路径")
    ck.add_argument("--provider", default=None, help="覆盖 agent provider(claude_cli/deepseek/fake)")
    ck.add_argument("--model", default=None, help="覆盖切块模型(默认 sonnet)")
    ck.add_argument("--manual", default=None, help="手动切块,形如 1-8,9-20(回合号),不走 agent")
    ck.set_defaults(func=cmd_chunk)

    xp = sub.add_parser("extract", help="提取(Prompt 2):对确认的段逐段提取五件套,落 staging")
    xp.add_argument("path", help="jsonl 路径(段取自该 session 的切块工作文件)")
    xp.add_argument("--provider", default=None, help="覆盖 agent provider(claude_cli/deepseek/fake)")
    xp.add_argument("--model", default=None, help="覆盖提取模型(默认 opus)")
    xp.add_argument("--seg", default=None, help="只提取指定段,形如 s1,s3(默认全部段)")
    xp.set_defaults(func=cmd_extract)

    cf = sub.add_parser("confirm", help="审核(S5):确认 staging episode 成 active 碎片 + 增量入库")
    cf.add_argument("path", help="jsonl 路径(staging 取自该 session)")
    cf.add_argument("--stage", default=None, help="确认指定 stage_id,如 e1")
    cf.add_argument("--all", action="store_true", help="确认该 session 全部 staging episode")
    cf.add_argument("--provider", default=None, help="覆盖 embedding provider(测试可 fake)")
    cf.set_defaults(func=cmd_confirm)

    rj = sub.add_parser("reject", help="审核(S5):拒一条 staging episode(留痕,不入库)")
    rj.add_argument("path", help="jsonl 路径")
    rj.add_argument("--stage", required=True, help="要拒的 stage_id,如 e1")
    rj.add_argument("--reason", default=None, help="拒绝原因(可选)")
    rj.set_defaults(func=cmd_reject)

    av = sub.add_parser("archive", help="审核(S5):把 active 碎片降级为 archived")
    av.add_argument("public_id", help="要归档的 episode public_id,如 ep_a1b2c3d4")
    av.set_defaults(func=cmd_archive)

    dl = sub.add_parser("delete", help="真删:从碎片正本 + DB 永久移除 episode 或 node(区别于 archive 软降级)")
    dlsub = dl.add_subparsers(dest="target", required=True)
    dle = dlsub.add_parser("episode", help="删一条 episode(碎片 + DB;因此变孤儿的 node 保留并点名)")
    dle.add_argument("public_id", help="episode public_id,如 ep_a1b2c3d4")
    dln = dlsub.add_parser("node", help="删一个 node(碎片 + DB;并从所有引用它的 episode 碎片摘除该引用)")
    dln.add_argument("label", help="node 的 label")
    dl.set_defaults(func=cmd_delete)

    rc = sub.add_parser("recall", help="检索记忆(细节/情景/概念)")
    rcsub = rc.add_subparsers(dest="action", required=True)
    rd = rcsub.add_parser("detail", help="细节检索:FTS 全文 grep + 开窗(逐字保真,不重构)")
    rd.add_argument("query", help="检索词(中文用 ≥3 字更可靠)")
    rd.add_argument("--since", default=None, help="只取该日期(含)之后创建的,ISO 串如 2026-06-01")
    rd.add_argument("--until", default=None, help="只取该日期(含)之前创建的")
    rd.add_argument("--raw", action="store_true", help="返回整条 source_text,不开窗")
    rd.add_argument("--json", action="store_true", help="机器可读输出(默认人类可读)")
    rd.add_argument("--limit", type=int, default=None, help="返回条数(默认 recall.detail_limit)")
    re_ = rcsub.add_parser("episode", help="情景检索:向量+FTS 双路 → RRF 融合 → 填槽")
    re_.add_argument("query", help="检索词/一句话描述想回忆的情景")
    re_.add_argument("--raw", action="store_true",
                     help="输出结构化槽位,不走重构(调试逃生口;默认重构成自然语言回忆)")
    re_.add_argument("--json", action="store_true", help="机器可读输出(默认人类可读)")
    re_.add_argument("--session", default=None,
                     help="会话标识:同 session 去重、跨 session 冷却(默认无=Phase 1 行为,不写台账)")
    rn = rcsub.add_parser("concept", help="概念检索:node/别名精确命中 → 膜 join 全量取(概念层,无原文)")
    rn.add_argument("node", help="node label 或别名(精确匹配;miss 时列相近 label)")
    rn.add_argument("--context", default=None,
                    help="一句话语境;给了按语境相似度排,不给按 tier/活跃度降序")
    rn.add_argument("--raw", action="store_true",
                    help="输出结构化条目,不走重构(调试逃生口;默认重构成综合立场)")
    rn.add_argument("--json", action="store_true", help="机器可读输出(默认人类可读)")
    rc.set_defaults(func=cmd_recall)

    op = sub.add_parser("opening", help="开场注入:重建/展示开场缓存(SessionStart hook 只读该缓存)")
    opsub = op.add_subparsers(dest="action", required=True)
    orb = opsub.add_parser("rebuild", help="重建开场缓存(默认仅当 .dirty 存在;--force 无视并强跑)")
    orb.add_argument("--force", action="store_true", help="无视 .dirty 强制重建")
    opsub.add_parser("show", help="展示开场缓存(不存在时提示先 rebuild)")
    op.set_defaults(func=cmd_opening)

    ixp = sub.add_parser("index", help="索引重建")
    ixsub = ixp.add_subparsers(dest="action", required=True)
    rb = ixsub.add_parser("rebuild", help="从碎片全量重建 DB(向量、FTS、膜)")
    rb.add_argument("--provider", choices=["fake", "dashscope"], default=None)
    ixp.set_defaults(func=cmd_index)

    dp = sub.add_parser("diagnose", help="实测平台事实")
    dp.add_argument("target", choices=["claude-code"], help="诊断目标")
    dp.set_defaults(func=cmd_diagnose)

    ep = sub.add_parser("embed", help="实测 embedding")
    ep.add_argument("text", nargs="+", help="要嵌的文本(可多条)")
    ep.add_argument("--provider", choices=["fake", "dashscope"], default=None)
    ep.set_defaults(func=cmd_embed)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
