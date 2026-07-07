"""情景检索(S6-3):向量+FTS 双路 → RRF 融合 → 硬过滤 → 衰减乘子 → 填槽。

s6_build_plan §4 S6-3 的十个要点按序落在 recall_episode 里:
  1. meta 锁检查(只读不写:照 index.assert_embeddable 的语义,锁在且不符即拒;锁缺不落锁)
  2. 双路召回(各取 topk_final*candidate_multiplier;vec0 不支持 filtered KNN,多取后应用层过滤;
     FTS 空手/坏 query 不报错,退化单路)
  3. 硬过滤 status='active'
  4. RRF:score = Σ_路 1/(rrf_k + rank),只用名次不用原始分
  5. 衰减乘子:final = rrf * (1 + w_activation * effective_activation)
  6. 主槽 topk_final 条
  7. 同源扩展槽:top-1 同 session、created_at 紧邻前后各 same_source_span 条(去掉已在主槽的)
  8. 联想槽:主槽经膜拿 node → 反查其他 active episode → 按与 query 的 overview 向量相似度排,
     记 via_nodes
  9. 组装:主槽带 source_text+summary+highlights;同源/联想槽只 summary+highlights;
     frame_nodes = 主槽 node label 集合(§5 契约)
 10. 时钟:只刷主槽 top-1 + 同源槽;联想槽不刷(被联想≠被回忆,裁定)

确定性边界(§0.2):本函数产出的候选集/槽位即"程序确定"的那一半,重构(S6-5)只做表达。
红线:输出手工挑字段,对外只 public_id / node label,无 uuid / 向量 / DB 整数 id。
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone

import sqlite_vec

from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.embedding import get_provider
from memory_system.log import get_logger
from memory_system.recall import decay
from memory_system.recall.detail import _fts_phrase


def _check_meta_lock(con: sqlite3.Connection, provider) -> None:
    """查询向量必须与库内向量同模型同维度(§2)。只读:锁缺失不落锁(区别于写侧)。"""
    has_meta = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall()) if has_meta else {}
    locked_model, locked_dim = meta.get("embedding_model"), meta.get("embedding_dim")
    if locked_model is None or locked_dim is None:
        return
    if provider.model != locked_model or str(provider.dim) != str(locked_dim):
        raise ValueError(
            f"embedding 配置与 meta 锁不符:provider={provider.model}/{provider.dim} ≠ "
            f"锁 {locked_model}/{locked_dim}。查询向量与库内向量必须同模型同维度,拒绝检索。")


def _l2(qvec: list[float], blob: bytes) -> float:
    """query 向量 vs 库内向量(float32 blob)的 L2 平方距离。单位向量下与余弦同序。"""
    vals = struct.unpack(f"{len(qvec)}f", blob)
    return sum((x - y) ** 2 for x, y in zip(qvec, vals))


def _highlights(row: sqlite3.Row) -> list:
    raw = row["highlights_json"]
    return json.loads(raw) if raw else []


def _alias_bridges(
    con: sqlite3.Connection, ep_ids: list[int], src_by_id: dict[int, str]
) -> dict[int, list[str]]:
    """别名露出锚定(裁定 §0.6):对给定 episode,取所挂 node 的全部别名(膜 join + node_aliases),
    逐个对该 episode 的库内 source_text 做 Python 子串判断——别名字面出现且规范 label 未字面出现
    → 收一行「文中「<alias>」= 概念 <label>」;两者都出现或都不出现都不收(别名不必管)。
    src_by_id 给 episode_id → 库内原文(判据用,不进对外输出;同源/联想槽输出虽不带原文,
    判据仍取 DB 侧原文)。返回 episode_id → 桥接行列表(去重 + 稳定排序);无桥接的不进字典。
    concept.py 入口的 alias_bridge(入口解析)语义不同,不在此列。
    """
    bridges: dict[int, list[str]] = {}
    if not ep_ids:
        return bridges
    ph = ",".join("?" * len(ep_ids))
    for r in con.execute(
            f"SELECT en.episode_id AS eid, n.label AS label, a.alias AS alias "
            f"FROM episode_nodes en JOIN nodes n ON n.id = en.node_id "
            f"JOIN node_aliases a ON a.node_id = n.id "
            f"WHERE en.episode_id IN ({ph})", list(ep_ids)):
        src = src_by_id.get(r["eid"]) or ""
        if r["alias"] in src and r["label"] not in src:
            line = f"文中「{r['alias']}」= 概念 {r['label']}"
            lst = bridges.setdefault(r["eid"], [])
            if line not in lst:
                lst.append(line)
    for eid in bridges:
        bridges[eid].sort()
    return bridges


def recall_episode(
    cfg: Config,
    query: str,
    *,
    touch: bool = True,
    now: datetime | None = None,
    session_key: str | None = None,
) -> dict:
    """情景检索。返回 §5 episode 契约:{mode, query, frame_nodes, slots:{primary/same_source/associative}}。

    `touch=False` 供 eval/只读场景关掉时钟刷新(§S6-8);默认刷 top-1+同源。
    meta 锁不符抛 ValueError(调用方决定怎么退出)。

    Phase 2 的 `session_key`(裁定 §0.2–0.5):
      - 为 None(CLI 手动/eval 夹具默认):完全等价 Phase 1——不去重、不冷却、不写日志。
      - 非空:同 session 已注入的 public_id 从**三槽全部**候选硬排除(去重,dedup_session 总开关);
        其他 session 在 cooldown_hours 窗口内注入过的候选 RRF×衰减分乘 cooldown_factor(冷却,温和降序);
        且 touch=True 时把返回三槽全部 public_id 写 injected_log(hit_at=now),与时钟刷新同一事务。
    """
    rc = cfg.recall
    now = now or datetime.now(timezone.utc)
    now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    empty = {"mode": "episode", "query": query, "frame_nodes": [],
             "slots": {"primary": [], "same_source": [], "associative": []}}
    q = (query or "").strip()
    if not q:
        return empty

    provider = get_provider(cfg.embedding)
    k = rc.topk_final * rc.candidate_multiplier
    con = connect(cfg.db_path)
    try:
        migrate.up(con)
        _check_meta_lock(con, provider)  # ① 只读校验,不符即拒

        # Phase 2:去重/冷却台账。无 session_key 全关(裁定 §0.4),两集皆空 = Phase 1 行为。
        dedup_pids: set[str] = set()      # 本 session 已注入(硬排除)
        cooldown_pids: set[str] = set()   # 其他 session 窗口内注入(软降序)
        if session_key:
            if rc.dedup_session:
                dedup_pids = {r["public_id"] for r in con.execute(
                    "SELECT DISTINCT public_id FROM injected_log WHERE session_key=?",
                    (session_key,))}
            if rc.cooldown_hours > 0 and rc.cooldown_factor != 1.0:
                window_start = (now_aware - timedelta(hours=rc.cooldown_hours)).isoformat()
                cooldown_pids = {r["public_id"] for r in con.execute(
                    "SELECT DISTINCT public_id FROM injected_log "
                    "WHERE session_key != ? AND hit_at >= ?",
                    (session_key, window_start))}

        # ② 双路召回,各取 k 条(过滤放应用层,两路对称)
        qvec = provider.embed([q])[0]
        vec_rank: dict[int, int] = {}
        for i, r in enumerate(con.execute(
                "SELECT episode_id, distance FROM episode_vectors "
                f"WHERE embedding MATCH ? AND k = {int(k)} ORDER BY distance",
                (sqlite_vec.serialize_float32(qvec),)), start=1):
            vec_rank[r["episode_id"]] = i
        fts_rank: dict[int, int] = {}
        try:
            for i, r in enumerate(con.execute(
                    "SELECT rowid FROM episode_fts WHERE episode_fts MATCH ? "
                    "ORDER BY bm25(episode_fts) LIMIT ?",
                    (_fts_phrase(q), k)), start=1):
                fts_rank[r["rowid"]] = i
        except sqlite3.OperationalError:
            fts_rank = {}  # FTS 空手/无有效 trigram:不报错,退化单路

        cand_ids = list(set(vec_rank) | set(fts_rank))
        if not cand_ids:
            return empty
        ph = ",".join("?" * len(cand_ids))
        rows = {r["id"]: r for r in con.execute(
            f"""SELECT id, public_id, overview, summary, highlights_json, source_text,
                       created_at, salience_tier, status, source_session_id,
                       last_accessed_at, activated_at
                FROM episodes WHERE id IN ({ph})""", cand_ids)}

        # ③ 硬过滤 active + session 去重(dedup_pids 空时退化为纯 active 过滤)。
        active = {eid: r for eid, r in rows.items()
                  if r["status"] == "active" and r["public_id"] not in dedup_pids}

        # ④ RRF(只用名次)+ ⑤ 衰减乘子(温和乘子,不是独立权重轴)
        final: dict[int, float] = {}
        for eid, r in active.items():
            rrf = 0.0
            if eid in vec_rank:
                rrf += 1.0 / (rc.rrf_k + vec_rank[eid])
            if eid in fts_rank:
                rrf += 1.0 / (rc.rrf_k + fts_rank[eid])
            act = decay.effective_activation(
                r["last_accessed_at"], r["salience_tier"], rc, now,
                activated_at=r["activated_at"], created_at=r["created_at"])
            final[eid] = rrf * (1.0 + rc.w_activation * act)

        # ⑤b 跨 session 冷却(温和乘子,裁定 §0.3):其他 session 窗口内注入过的候选分乘 cooldown_factor。
        #     只降序不排除——回忆线索永远响应。冷却状态落日志(可重放红线),但不进 --json 契约。
        if cooldown_pids:
            cooled = sorted(active[eid]["public_id"] for eid in final
                            if active[eid]["public_id"] in cooldown_pids)
            for eid in final:
                if active[eid]["public_id"] in cooldown_pids:
                    final[eid] *= rc.cooldown_factor
            if cooled:
                get_logger().info(
                    "recall episode 冷却生效(可重放): session=%s factor=%s window_h=%s cooled=%s",
                    session_key, rc.cooldown_factor, rc.cooldown_hours,
                    json.dumps(cooled, ensure_ascii=False))

        # ⑥ 主槽:final 降序取 topk_final(同分按 created_at/public_id 定序,保证可重放)
        primary_ids = sorted(
            final, key=lambda i: (-final[i], active[i]["created_at"], active[i]["public_id"])
        )[:rc.topk_final]

        # ⑦ 同源扩展槽:top-1 同 session、时间紧邻前后各 span 条(去掉已在主槽的)
        same_rows: list[sqlite3.Row] = []
        if primary_ids:
            ssid = active[primary_ids[0]]["source_session_id"]
            if ssid:
                sess = con.execute(
                    "SELECT id, public_id, summary, highlights_json, created_at "
                    "FROM episodes WHERE source_session_id=? AND status='active' "
                    "ORDER BY created_at, id", (ssid,)).fetchall()
                pos = next((i for i, r in enumerate(sess) if r["id"] == primary_ids[0]), None)
                if pos is not None:
                    span = rc.same_source_span
                    neigh = sess[max(0, pos - span):pos] + sess[pos + 1:pos + 1 + span]
                    same_rows = [r for r in neigh if r["id"] not in set(primary_ids)
                                 and r["public_id"] not in dedup_pids]  # 同源槽同样硬去重

        # ⑧ 联想槽:主槽经膜拿 node → 反查 → 按与 query 的 overview 向量相似度排
        frame_labels: list[str] = []
        assoc_rows: list[sqlite3.Row] = []
        via: dict[int, list[str]] = {}
        if primary_ids:
            php = ",".join("?" * len(primary_ids))
            node_rows = con.execute(
                f"SELECT DISTINCT n.id, n.label FROM episode_nodes en "
                f"JOIN nodes n ON n.id=en.node_id WHERE en.episode_id IN ({php})",
                primary_ids).fetchall()
            frame_labels = sorted(r["label"] for r in node_rows)
            node_ids = [r["id"] for r in node_rows]
            if node_ids:
                exclude = set(primary_ids) | {r["id"] for r in same_rows}
                phn = ",".join("?" * len(node_ids))
                cands = [r for r in con.execute(
                    f"SELECT DISTINCT e.id, e.public_id, e.summary, e.highlights_json "
                    f"FROM episodes e JOIN episode_nodes en ON en.episode_id=e.id "
                    f"WHERE en.node_id IN ({phn}) AND e.status='active'", node_ids)
                    if r["id"] not in exclude and r["public_id"] not in dedup_pids]
                if cands:
                    phc = ",".join("?" * len(cands))
                    dist = {r["episode_id"]: _l2(qvec, r["embedding"]) for r in con.execute(
                        f"SELECT episode_id, embedding FROM episode_vectors "
                        f"WHERE episode_id IN ({phc})", [r["id"] for r in cands])}
                    cands.sort(key=lambda r: (dist.get(r["id"], float("inf")), r["public_id"]))
                    assoc_rows = cands[:rc.assoc_limit]
                    pha = ",".join("?" * len(assoc_rows))
                    for r in con.execute(
                            f"SELECT en.episode_id, n.label FROM episode_nodes en "
                            f"JOIN nodes n ON n.id=en.node_id "
                            f"WHERE en.episode_id IN ({pha}) AND en.node_id IN ({phn})",
                            [r["id"] for r in assoc_rows] + node_ids):
                        via.setdefault(r["episode_id"], []).append(r["label"])

        # ⑧b 别名桥接(裁定 §0.6):三槽每条 episode,别名字面出现且规范 label 未出现 → 附桥接行。
        #     同源/联想槽输出不带 source_text,判据仍 grep 库内 source_text(DB 侧现取)。
        bridge_ids = (list(primary_ids) + [r["id"] for r in same_rows]
                      + [r["id"] for r in assoc_rows])
        src_by_id: dict[int, str] = {i: active[i]["source_text"] for i in primary_ids}
        miss_src = [i for i in bridge_ids if i not in src_by_id]
        if miss_src:
            phs = ",".join("?" * len(miss_src))
            for r in con.execute(
                    f"SELECT id, source_text FROM episodes WHERE id IN ({phs})", miss_src):
                src_by_id[r["id"]] = r["source_text"]
        bridges = _alias_bridges(con, bridge_ids, src_by_id)

        # ⑨ 组装(手工挑字段红线;主槽带原文,同源/联想只 summary 级)
        #    alias_bridges 无桥接时省略该键(契约选择:省略而非空列表,三槽一致)。
        primary = [{
            "public_id": active[i]["public_id"], "overview": active[i]["overview"],
            "summary": active[i]["summary"], "highlights": _highlights(active[i]),
            "source_text": active[i]["source_text"], "created_at": active[i]["created_at"],
            "salience_tier": active[i]["salience_tier"], "score": round(final[i], 6),
            **({"alias_bridges": bridges[i]} if bridges.get(i) else {}),
        } for i in primary_ids]
        same_source = [{
            "public_id": r["public_id"], "summary": r["summary"],
            "highlights": _highlights(r), "created_at": r["created_at"],
            **({"alias_bridges": bridges[r["id"]]} if bridges.get(r["id"]) else {}),
        } for r in same_rows]
        associative = [{
            "public_id": r["public_id"], "summary": r["summary"],
            "highlights": _highlights(r), "via_nodes": sorted(via.get(r["id"], [])),
            **({"alias_bridges": bridges[r["id"]]} if bridges.get(r["id"]) else {}),
        } for r in assoc_rows]

        # ⑩ 时钟:只刷 top-1 + 同源;联想槽不刷(裁定,不是建议)。
        #    Phase 2:session_key 非空且 touch=True → 三槽全部 public_id 写 injected_log,与时钟同一事务。
        if touch and primary_ids:
            decay.touch_episodes(con, [primary_ids[0]] + [r["id"] for r in same_rows], now)
            if session_key:
                inj_pids = ([p["public_id"] for p in primary]
                            + [s["public_id"] for s in same_source]
                            + [a["public_id"] for a in associative])
                hit_at = now_aware.isoformat()
                con.executemany(
                    "INSERT INTO injected_log(session_key, public_id, tool, hit_at) "
                    "VALUES (?, ?, 'episode', ?)",
                    [(session_key, pid, hit_at) for pid in inj_pids])
            con.commit()
    finally:
        con.close()

    return {"mode": "episode", "query": query, "frame_nodes": frame_labels,
            "slots": {"primary": primary, "same_source": same_source,
                      "associative": associative}}
