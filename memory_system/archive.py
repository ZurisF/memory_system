"""归档引擎 —— staging 五件套 → active 碎片正本 + 增量同步 SQLite(S5 入库闭环)。

碎片是正本(idea_v2 §12.13):confirm 把这一条增量插进 DB(episode + 膜 + 向量,FTS 触发器
自动),DB 失败则**不写任何碎片(含 node)、staging 原封不动**,可干净重试;删库后
`index rebuild` 仍能从碎片无损还原。

落地顺序硬约束:node 三选一先只在内存里规划(`_plan_nodes`),所有可失败动作(embedding、
向量插入、DB 约束)都在事务内做完并 commit 之后,才把 node 碎片与 episode 碎片原子写盘、再清
staging。否则 DB 阶段失败会留下未确认的 node 碎片/别名,污染后续提取 agent 的 existing_nodes。

动作(idea_v2 §9 两条退场通道 + 误入库的真删):
  confirm  staging →(人工确认)→ active   :node 三选一落地(别名合并)、写碎片、增量插 DB,清 staging。
  reject   staging →(人工拒)→ rejected   :从 staging 移除留痕,不写碎片不进 DB。
  archive  active  →(人工)→ archived      :改已 active 碎片 status,DB 同步;不再被检索注入(软降级,正本仍在)。
  delete   碎片 + DB → 永久移除            :误入库时真删 episode / node 正本及其索引(区别于 archive)。

**删除的落地顺序与 confirm 相反,且这是对的**:删除时碎片正本必须先走且权威——
先删碎片再删 DB。中途失败最坏只留一条「悬空 DB 行」(`doctor` 能对账出、`index rebuild` 会丢弃),
绝不会出现「删了又被 rebuild 复活」。真删 node 还必须把该 label 从所有引用它的 episode 碎片里摘掉,
否则 rebuild 会因 episode 碎片仍引用而用 ensure_node 把它复活成桩节点。

uuid 绝不进碎片(§5):staging 的 covered_uuids 只是工作态,confirm 不带它进 episode。
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from memory_system import staging_store
from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.embedding.base import EmbeddingProvider
from memory_system.fragments import (
    Episode,
    Node,
    episode_path,
    load_all_episodes,
    node_path,
    read_episode,
    read_node,
    write_episode,
    write_node,
)
from memory_system.index import assert_embeddable, insert_episode


class ArchiveError(RuntimeError):
    """归档动作的可预期失败(无此 staging 条目、无 active 碎片、向量维度不符等)。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_public_id(episodes_dir: Path) -> str:
    """生成不与现有碎片相撞的 public_id(ep_<8hex>);随机、不依赖顺序、删库重建不变。"""
    for _ in range(100):
        pid = "ep_" + secrets.token_hex(4)
        if not episode_path(episodes_dir, pid).exists():
            return pid
    raise ArchiveError("public_id 连续 100 次相撞,异常")  # 4 字节随机,实务不可达


# ---- node 三选一规划(纯内存,确认时别名合并生效;DB 成功后才落盘)----

def _plan_nodes(cfg: Config, staging_nodes: list[dict]) -> tuple[list[str], dict[str, Node]]:
    """规划 staging 的 nodes(带 action/new_alias)三选一,**不落盘**。

    返回 `(去重膜 label 列表, {label: 待写 Node 最终态})`。`planned` 只含需要新建或改别名的
    node;已存在且复用(match_existing)的不在内,留待 DB 同步时读现有碎片。落盘推迟到
    `confirm_episode` 里 DB commit 成功之后,保证失败不留碎片。

    - new:无碎片则规划新建。
    - add_alias:目标碎片在则并入别名(去重后规划回写);目标不在则退化为带该别名的新建。
    - match_existing:目标碎片在则复用(不进 planned);缺则补建空 node(膜不悬空)。
    """
    labels: list[str] = []
    planned: dict[str, Node] = {}
    for nd in staging_nodes:
        label = nd.get("label")
        if not label:
            continue
        action = nd.get("action", "new")
        p = node_path(cfg.nodes_dir, label)
        alias = (nd.get("new_alias") or "").strip()
        if action == "add_alias" and p.exists():
            node = read_node(p)
            if alias and alias not in node.aliases:
                node.aliases.append(alias)
                node.updated_at = _now()
                planned[label] = node
        elif not p.exists():
            now = _now()
            planned[label] = Node(
                label=label, created_at=now, updated_at=now,
                aliases=[alias] if (action == "add_alias" and alias) else [],
            )
        if label not in labels:
            labels.append(label)
    return labels, planned


def _upsert_node_db(
    con: sqlite3.Connection, cfg: Config, label: str, planned: Node | None
) -> int:
    """把 label 的 node 同步进 DB(节点行 + 别名,幂等),返回 node_id。

    优先用本次 confirm 规划的内存态 `planned`(碎片此刻还没落盘);为 None 表示 match_existing
    复用已有碎片,从盘上读。
    """
    row = con.execute("SELECT id FROM nodes WHERE label=?", (label,)).fetchone()
    p = node_path(cfg.nodes_dir, label)
    node = planned or (read_node(p) if p.exists() else None)
    if row:
        nid = row[0]
    elif node:
        nid = con.execute(
            "INSERT INTO nodes(label,type,created_at,updated_at) VALUES(?,?,?,?)",
            (node.label, node.type, node.created_at, node.updated_at),
        ).lastrowid
    else:
        now = _now()
        nid = con.execute(
            "INSERT INTO nodes(label,type,created_at,updated_at) VALUES(?,?,?,?)",
            (label, None, now, now),
        ).lastrowid
    if node:
        for al in node.aliases:
            con.execute("INSERT OR IGNORE INTO node_aliases(alias,node_id) VALUES(?,?)", (al, nid))
    return nid


# ---- confirm:staging → active ----

def confirm_episode(
    cfg: Config, session_id: str, stage_id: str, emb_provider: EmbeddingProvider
) -> str:
    """确认一条 staging episode 成 active 碎片正本 + 增量插 DB,返回 public_id。"""
    sdir = cfg.staging_episodes_dir
    doc = staging_store.load(sdir, session_id)
    if not doc:
        raise ArchiveError(f"无 staging 文档: {session_id}")
    ep_doc = next((e for e in doc.get("episodes", []) if e.get("stage_id") == stage_id), None)
    if ep_doc is None:
        raise ArchiveError(f"staging 无此 episode: {stage_id}")

    public_id = _new_public_id(cfg.episodes_dir)
    frag_path = episode_path(cfg.episodes_dir, public_id)  # 仅算路径,DB 成功后才落文件
    now = _now()

    # node 规划(别名合并算定,纯内存不落盘;DB 成功后才写碎片)
    labels, planned = _plan_nodes(cfg, ep_doc.get("nodes") or [])

    ep = Episode(
        public_id=public_id,
        overview=ep_doc["overview"],
        summary=ep_doc["summary"],
        source_text=ep_doc["source_text"],
        salience_tier=int(ep_doc.get("salience_tier") or 1),
        status="active",
        created_at=ep_doc.get("created_at") or now,  # 发生时间;缺则确认时间兜底
        highlights=list(ep_doc.get("highlights") or []),
        keywords=[],  # 提取五件套不产 keywords(Phase 1),留空
        nodes=labels,
        activated_at=now,
        source_session_id=session_id,
        source_path=doc.get("source_path"),
    )

    # 增量插 DB:任一步失败回滚,不写 episode 碎片、不动 staging → 可干净重试。
    con = connect(cfg.db_path)
    try:
        migrate.up(con)
        model, dim = assert_embeddable(con, emb_provider)
        vec = emb_provider.embed([ep.overview])[0]
        if len(vec) != dim:
            raise ArchiveError(f"overview 向量维度 {len(vec)} ≠ meta 锁 {dim},拒写")
        eid = insert_episode(con, ep, frag_path, model, dim)  # FTS 触发器随 INSERT 同步
        for label in labels:
            nid = _upsert_node_db(con, cfg, label, planned.get(label))
            con.execute(
                "INSERT OR IGNORE INTO episode_nodes(episode_id,node_id) VALUES(?,?)", (eid, nid)
            )
        con.execute(
            "INSERT INTO episode_vectors(episode_id,embedding) VALUES(?,?)",
            (eid, sqlite_vec.serialize_float32(vec)),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    # DB 落定,才把碎片正本原子写盘:先 node 三选一(规划态),再 episode,最后清 staging。
    for node in planned.values():
        write_node(cfg.nodes_dir, node)
    write_episode(cfg.episodes_dir, ep)
    staging_store.remove_episode(sdir, session_id, stage_id)
    from memory_system.recall import opening  # active 集变动 → 开场缓存过期(S6-6)
    opening.mark_dirty(cfg)
    return public_id


def confirm_all(
    cfg: Config, session_id: str, emb_provider: EmbeddingProvider
) -> list[str]:
    """确认该 session 全部 staging episode(逐条;某条失败即抛,已确认的不回退)。"""
    doc = staging_store.load(cfg.staging_episodes_dir, session_id)
    stage_ids = [e["stage_id"] for e in (doc.get("episodes") if doc else [])]
    out = []
    for sid in stage_ids:
        out.append(confirm_episode(cfg, session_id, sid, emb_provider))
    return out


# ---- reject:staging → rejected ----

def reject_episode(cfg: Config, session_id: str, stage_id: str,
                   reason: str | None = None) -> None:
    try:
        staging_store.reject_episode(cfg.staging_episodes_dir, session_id, stage_id, reason)
    except KeyError as e:
        raise ArchiveError(str(e)) from None


# ---- archive:active → archived ----

def archive_episode(cfg: Config, public_id: str) -> None:
    """把一条 active 碎片降级为 archived(碎片 + DB 同步);不再被检索注入。"""
    p = episode_path(cfg.episodes_dir, public_id)
    if not p.exists():
        raise ArchiveError(f"无此碎片: {public_id}")
    ep = read_episode(p)
    if ep.status == "archived":
        return
    ep.status = "archived"
    ep.archived_at = _now()
    write_episode(cfg.episodes_dir, ep)
    con = connect(cfg.db_path)
    try:
        migrate.up(con)
        con.execute(
            "UPDATE episodes SET status='archived', archived_at=? WHERE public_id=?",
            (ep.archived_at, public_id),
        )
        con.commit()
    finally:
        con.close()
    from memory_system.recall import opening  # active→archived 改变 active 集(S6-6)
    opening.mark_dirty(cfg)


# ---- delete:碎片 + DB 永久移除(误入库的真删,区别于 archive 软降级)----


@dataclass
class DeleteReport:
    """删除结果(供 CLI / API 回显、给用户决定后续清理)。"""

    public_id: str | None = None         # 删的 episode
    label: str | None = None             # 删的 node
    orphaned_nodes: list[str] = field(default_factory=list)        # 删 episode 后变孤儿(不再挂任何 episode)的 node
    dereferenced_episodes: list[str] = field(default_factory=list)  # 删 node 时被摘除该引用的 episode public_id


def delete_episode(cfg: Config, public_id: str) -> DeleteReport:
    """真删一条 episode:碎片正本 + DB(向量/膜/FTS)。孤儿 node 保留,在报告里点名。

    落地顺序(与 confirm 相反):**先删碎片正本**,再删 DB。中途失败最坏剩悬空 DB 行,
    `doctor` 能对账、`index rebuild` 会丢弃——绝不会被 rebuild 复活。
    """
    p = episode_path(cfg.episodes_dir, public_id)
    if not p.exists():
        raise ArchiveError(f"无此碎片: {public_id}")
    ep = read_episode(p)            # 先记下它的 nodes,删后据此算孤儿
    p.unlink()                      # 正本先走

    con = connect(cfg.db_path)
    try:
        migrate.up(con)
        row = con.execute("SELECT id FROM episodes WHERE public_id=?", (public_id,)).fetchone()
        if row:
            eid = row[0]
            con.execute("DELETE FROM episode_vectors WHERE episode_id=?", (eid,))  # vec0 不吃 FK,显式删
            con.execute("DELETE FROM episodes WHERE id=?", (eid,))  # 级联清膜;触发器清 FTS
            con.commit()
        # 孤儿:该 episode 曾引用、删后 DB 里不再被任何 episode 挂载、且 node 碎片仍在的 label
        orphaned: list[str] = []
        for label in ep.nodes:
            (cnt,) = con.execute(
                "SELECT COUNT(*) FROM episode_nodes en JOIN nodes n ON n.id=en.node_id "
                "WHERE n.label=?",
                (label,),
            ).fetchone()
            if cnt == 0 and node_path(cfg.nodes_dir, label).exists():
                orphaned.append(label)
    finally:
        con.close()
    from memory_system.recall import opening  # 删 active 改变 active 集(S6-6)
    opening.mark_dirty(cfg)
    return DeleteReport(public_id=public_id, orphaned_nodes=orphaned)


def delete_node(cfg: Config, label: str) -> DeleteReport:
    """真删一个 node:碎片正本 + DB(节点/别名/膜),并从所有引用它的 episode 碎片摘除该 label。

    摘除引用是**正确性关键**:不摘,`index rebuild` 会因 episode 碎片仍引用而用 `ensure_node`
    把该 node 复活成桩节点。顺序:先回写 episode 碎片(摘 label)→ 删 node 碎片 → 删 DB,
    每步幂等/收敛,中途失败由 `doctor` + `index rebuild` 兜底收口。
    """
    np_ = node_path(cfg.nodes_dir, label)
    if not np_.exists():
        raise ArchiveError(f"无此 node 碎片: {label}")

    # 1) 从所有引用它的 episode 碎片里摘掉 label 并回写(否则 rebuild 复活成桩)
    dereferenced: list[str] = []
    for ep_path, ep in load_all_episodes(cfg.episodes_dir):
        if label in ep.nodes:
            ep.nodes = [lb for lb in ep.nodes if lb != label]
            write_episode(cfg.episodes_dir, ep)
            dereferenced.append(ep.public_id)

    # 2) 删 node 碎片正本(必须删,否则 rebuild 会把孤立 node 碎片重新灌回 DB)
    np_.unlink()

    # 3) DB:删 node 行 → FK 级联清 node_aliases 与 episode_nodes 膜
    con = connect(cfg.db_path)
    try:
        migrate.up(con)
        con.execute("DELETE FROM nodes WHERE label=?", (label,))
        con.commit()
    finally:
        con.close()
    return DeleteReport(label=label, dereferenced_episodes=dereferenced)
