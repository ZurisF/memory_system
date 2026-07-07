"""查看侧只读查询:列表 / 详情 / node↔node 共现边。

只读 DB + 碎片,不写。两条红线(idea_v2):
  - uuid / 向量永不出现在返回里(Episode/Node dataclass 本就无 uuid 字段)。
  - source_text 只在单条详情(read_memory)给,列表不带。

node↔node 边按 idea_v2 §17/§96 的正典:**两个 node 的关联由"共享它们的情景"蕴含**。
这里按 episode_nodes 共现现算(同一 episode 的多个 node 两两连边),`via` 即作解释的
共享情景 public_id。独立 `edges` 表是 idea_v2 明确"降级为可选缓存",本轮不建、入口保留:
以后此函数改读缓存表即可,API 形状不变。
"""

from __future__ import annotations

import re
from itertools import combinations

from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.fragments import episode_path, node_path, read_episode, read_node

# public_id 同时支持线上随机 ID(ep_<8hex>)和评测合成 ID(ep_syn####)。
# 仍只允许文件名安全字符,防止 /api/memory 被拿来穿越 episodes_dir。
_PUBLIC_ID_RE = re.compile(r"^ep_[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def list_memories(cfg: Config, include_archived: bool = False) -> dict:
    """galaxy / 列表数据源。episodes(+膜)、nodes(label/type/aliases/episode_count)、
    membrane、edges(共现)。统计与边都只就**当前展示的 episode 集**算,保持一致。"""
    con = connect(cfg.db_path)
    try:
        migrate.up(con)
        where = "" if include_archived else "WHERE e.status='active'"
        ep_rows = con.execute(
            f"""SELECT e.id, e.public_id, e.overview, e.salience_tier, e.status,
                       e.created_at, e.activated_at, e.source_session_id
                FROM episodes e {where}
                ORDER BY e.created_at"""
        ).fetchall()
        eids = {r[0] for r in ep_rows}

        # episode_id -> [label](只取当前展示集内的膜)
        ep_labels: dict[int, list[str]] = {}
        for eid, label in con.execute(
            "SELECT en.episode_id, n.label FROM episode_nodes en "
            "JOIN nodes n ON n.id = en.node_id"
        ).fetchall():
            if eid in eids:
                ep_labels.setdefault(eid, []).append(label)

        episodes: list[dict] = []
        membrane: list[dict] = []
        pub_of = {}
        for eid, pub, ov, tier, status, created, activated, ssid in ep_rows:
            labels = sorted(ep_labels.get(eid, []))
            pub_of[eid] = pub
            episodes.append({
                "public_id": pub, "overview": ov, "salience_tier": tier,
                "status": status, "created_at": created, "activated_at": activated,
                "source_session_id": ssid, "nodes": labels,
            })
            for lb in labels:
                membrane.append({"public_id": pub, "label": lb})

        # nodes:label 正本在碎片,但 type/aliases/count 用 DB 足够列表用(碎片与 DB 由写口保持同步)
        nodes: list[dict] = []
        alias_of: dict[int, list[str]] = {}
        for nid, alias in con.execute(
            "SELECT node_id, alias FROM node_aliases"
        ).fetchall():
            alias_of.setdefault(nid, []).append(alias)
        # 当前展示集内每个 label 的挂载数
        count_of: dict[str, int] = {}
        for labels in ep_labels.values():
            for lb in labels:
                count_of[lb] = count_of.get(lb, 0) + 1
        for nid, label, ntype in con.execute(
            "SELECT id, label, type FROM nodes ORDER BY label"
        ).fetchall():
            nodes.append({
                "label": label, "type": ntype,
                "aliases": sorted(alias_of.get(nid, [])),
                "episode_count": count_of.get(label, 0),
            })

        # 共现边:同一 episode 的 label 两两连;via 累计该边的共享情景。
        # NOTICE 当前为 O(E·K²) 实时计算(K=单 episode 平均 node 数,E=episode 总数),
        # 每次 /api/memories 请求都重算。库规模增长后应建物化 edges 缓存表,
        # 在 confirm_episode / index rebuild 时增量更新,API 形状不变。
        # 详见 idea_v2 §17/§96–98 及 HANDOFF_NOTES.md。
        edge_via: dict[tuple[str, str], list[str]] = {}
        for eid, labels in ep_labels.items():
            for a, b in combinations(sorted(set(labels)), 2):
                edge_via.setdefault((a, b), []).append(pub_of[eid])
        edges = [{"a": a, "b": b, "via": via} for (a, b), via in edge_via.items()]
    finally:
        con.close()

    return {"episodes": episodes, "nodes": nodes, "membrane": membrane, "edges": edges}


def read_memory(cfg: Config, public_id: str) -> dict | None:
    """单条 active/archived episode 详情(对齐 §8 ActiveEpisode)。碎片是正本。"""
    if not _PUBLIC_ID_RE.match(public_id or ""):
        return None
    p = episode_path(cfg.episodes_dir, public_id)
    if not p.exists():
        return None
    ep = read_episode(p)
    return {
        "public_id": ep.public_id, "overview": ep.overview, "summary": ep.summary,
        "source_text": ep.source_text, "salience_tier": ep.salience_tier,
        "status": ep.status, "created_at": ep.created_at,
        "highlights": ep.highlights, "keywords": ep.keywords, "nodes": ep.nodes,
        "activated_at": ep.activated_at, "archived_at": ep.archived_at,
        "source_session_id": ep.source_session_id, "source_path": ep.source_path,
    }


def read_node_detail(cfg: Config, label: str) -> dict | None:
    """node 详情 + 挂载的 active episodes。label/type/aliases 以碎片正本为准。"""
    p = node_path(cfg.nodes_dir, label)
    if not p.exists():
        return None
    nd = read_node(p)
    con = connect(cfg.db_path)
    try:
        migrate.up(con)
        row = con.execute("SELECT id FROM nodes WHERE label=?", (label,)).fetchone()
        eps: list[dict] = []
        if row:
            for pub, ov, tier in con.execute(
                """SELECT e.public_id, e.overview, e.salience_tier
                   FROM episodes e JOIN episode_nodes en ON en.episode_id = e.id
                   WHERE en.node_id = ? AND e.status = 'active'
                   ORDER BY e.created_at""",
                (row[0],),
            ).fetchall():
                eps.append({"public_id": pub, "overview": ov, "salience_tier": tier})
    finally:
        con.close()
    return {
        "label": nd.label, "type": nd.type, "aliases": nd.aliases,
        "created_at": nd.created_at, "updated_at": nd.updated_at, "episodes": eps,
    }
