"""归档引擎 —— staging 五件套 → active 碎片正本 + 增量同步 SQLite(S5 入库闭环)。

碎片是正本(idea_v2 §12.13):confirm 把这一条增量插进 DB(episode + 膜 + 向量,FTS 触发器
自动),DB 失败则不留 episode 碎片、staging 原封不动,可干净重试;删库后 `index rebuild` 仍能
从碎片无损还原。

三个动作(idea_v2 §9 两条退场通道):
  confirm  staging →(人工确认)→ active   :node 三选一落地(别名合并)、写碎片、增量插 DB,清 staging。
  reject   staging →(人工拒)→ rejected   :从 staging 移除留痕,不写碎片不进 DB。
  archive  active  →(人工)→ archived      :改已 active 碎片 status,DB 同步;不再被检索注入。

uuid 绝不进碎片(§5):staging 的 covered_uuids 只是工作态,confirm 不带它进 episode。
"""

from __future__ import annotations

import secrets
import sqlite3
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


# ---- node 三选一落地(碎片层,确认时别名合并生效)----

def _land_nodes(cfg: Config, staging_nodes: list[dict]) -> list[str]:
    """把 staging 的 nodes(带 action/new_alias)落成 node 碎片,返回去重的膜 label 列表。

    - new:无碎片则新建。
    - add_alias:目标碎片在则并入别名(去重);目标不在则退化为带该别名的新建。
    - match_existing:目标碎片应在;缺则补建空 node(膜不悬空)。
    """
    labels: list[str] = []
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
                write_node(cfg.nodes_dir, node)
        elif not p.exists():
            now = _now()
            write_node(cfg.nodes_dir, Node(
                label=label, created_at=now, updated_at=now,
                aliases=[alias] if (action == "add_alias" and alias) else [],
            ))
        if label not in labels:
            labels.append(label)
    return labels


def _upsert_node_db(con: sqlite3.Connection, cfg: Config, label: str) -> int:
    """把 label 的 node 碎片同步进 DB(节点行 + 别名,幂等),返回 node_id。"""
    row = con.execute("SELECT id FROM nodes WHERE label=?", (label,)).fetchone()
    p = node_path(cfg.nodes_dir, label)
    node = read_node(p) if p.exists() else None
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

    # node 落地(别名合并;幂等,DB 失败也无害)
    labels = _land_nodes(cfg, ep_doc.get("nodes") or [])

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
            nid = _upsert_node_db(con, cfg, label)
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

    # DB 落定,才写正本碎片,再清 staging(工作态消费完)
    write_episode(cfg.episodes_dir, ep)
    staging_store.remove_episode(sdir, session_id, stage_id)
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
