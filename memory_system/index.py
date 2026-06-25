"""index rebuild —— 从碎片全量重建 DB(idea_v2 §12.13)。

真相只在碎片;DB(含膜、向量、FTS)全是可重建索引。本模块把碎片读回,
重灌 nodes/aliases/episodes/膜,重嵌 overview 写 vec0,FTS 由触发器自动同步。

向量写入前校验 model/dim 与 meta 锁一致,不符拒写(不补零不截断,idea_v2 §12.12)。

运行态不还原:`last_accessed_at` 重置为 activated_at(衰减时钟非记忆正本);
`processed_messages` 推迟到 S2,本步不涉及。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.embedding.base import EmbeddingProvider
from memory_system.fragments import (
    Episode,
    load_all_episodes,
    load_all_nodes,
)


@dataclass
class RebuildReport:
    nodes: int = 0
    aliases: int = 0
    episodes: int = 0
    membrane: int = 0
    vectors: int = 0
    stub_nodes: list[str] = None  # 碎片引用了但无 node 碎片的 label

    def __post_init__(self) -> None:
        if self.stub_nodes is None:
            self.stub_nodes = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def assert_embeddable(con: sqlite3.Connection, provider: EmbeddingProvider, *, lock_meta: bool = True) -> tuple[str, int]:
    """建锁或校验:meta 无锁则按 provider 落锁(bookkeeping-on-write,让 rebuild 删库后单命令自足);
    有锁则校验 provider 的 model/dim 一致,不符即拒。返回 (model, dim)。

    lock_meta=False 时,锁缺失则跳过写锁(仅返回 provider 的 model/dim 供本次会话使用),
    用于 fake provider 等不应持久化锁的场景,避免把真实 DB 锁成 fake。
    """
    has_meta = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not has_meta:
        raise ValueError("meta 表不存在,schema 未迁移到 001。")
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    locked_model = meta.get("embedding_model")
    locked_dim = meta.get("embedding_dim")
    if locked_model is None or locked_dim is None:
        if not lock_meta:
            return provider.model, provider.dim
        # 锁缺失(如删库重建后):按当前 provider 落锁,记"vec 表里装的是什么"
        con.execute(
            "INSERT INTO meta(key,value) VALUES('embedding_model',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (provider.model,),
        )
        con.execute(
            "INSERT INTO meta(key,value) VALUES('embedding_dim',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(provider.dim),),
        )
        con.commit()
        return provider.model, provider.dim
    if provider.model != locked_model:
        raise ValueError(
            f"embedding 模型不符:provider={provider.model!r} ≠ meta 锁 {locked_model!r}。"
            "换模型需从碎片全量重嵌,不可混写。"
        )
    if str(provider.dim) != str(locked_dim):
        raise ValueError(
            f"embedding 维度不符:provider={provider.dim} ≠ meta 锁 {locked_dim}。"
            "不补零不截断,拒绝写向量。"
        )
    return locked_model, int(locked_dim)


def _clear(con: sqlite3.Connection) -> None:
    """清空内容表。删 nodes/episodes 经 FK 级联清膜;FTS 由触发器随 episodes 清。"""
    con.execute("DELETE FROM episode_vectors")
    con.execute("DELETE FROM episodes")   # 触发器清 FTS;级联清膜
    con.execute("DELETE FROM nodes")      # 级联清 aliases、残膜
    con.commit()


def _insert_nodes(
    con: sqlite3.Connection, nodes: list, report: RebuildReport
) -> dict[str, int]:
    """灌已 parse 的 node 列表 → nodes/aliases,返回 label → node_id。"""
    label_to_id: dict[str, int] = {}
    for _path, nd in nodes:
        cur = con.execute(
            "INSERT INTO nodes(label, type, created_at, updated_at) VALUES (?,?,?,?)",
            (nd.label, nd.type, nd.created_at, nd.updated_at),
        )
        nid = cur.lastrowid
        label_to_id[nd.label] = nid
        report.nodes += 1
        for alias in nd.aliases:
            con.execute(
                "INSERT INTO node_aliases(alias, node_id) VALUES (?,?)", (alias, nid)
            )
            report.aliases += 1
    return label_to_id


def ensure_node(
    con: sqlite3.Connection, label: str, label_to_id: dict[str, int], report: RebuildReport
) -> int:
    """膜引用了一个没有 node 碎片的 label → 建桩 node,保证膜不悬空。"""
    if label in label_to_id:
        return label_to_id[label]
    now = _now()
    cur = con.execute(
        "INSERT INTO nodes(label, type, created_at, updated_at) VALUES (?,?,?,?)",
        (label, None, now, now),
    )
    nid = cur.lastrowid
    label_to_id[label] = nid
    report.nodes += 1
    report.stub_nodes.append(label)
    return nid


def _embed_overviews(
    cfg: Config, provider: EmbeddingProvider, eps: list, dim: int
) -> list[list[float]]:
    """联网重嵌所有 overview(分批 ≤ batch_size),返回与 eps 对齐的向量列表。

    在碰 DB 内容之前调用:网络失败/维度不符在此抛,不会留下半清空的库。
    """
    overviews = [ep.overview for _path, ep in eps]
    vectors: list[list[float]] = []
    bs = max(1, cfg.embedding.batch_size)
    for i in range(0, len(overviews), bs):
        batch = overviews[i : i + bs]
        for vec in provider.embed(batch):
            if len(vec) != dim:
                raise ValueError(f"overview 向量维度 {len(vec)} ≠ meta 锁 {dim},拒写")
            vectors.append(vec)
    return vectors


def _insert_episodes(
    con: sqlite3.Connection,
    eps: list,
    vectors: list[list[float]],
    label_to_id: dict[str, int],
    model: str,
    dim: int,
    report: RebuildReport,
) -> None:
    """灌已 parse 的 episode 列表 + 膜 + 已算好的向量(纯 DB 写,不再失败)。"""
    for (path, ep), vec in zip(eps, vectors):
        eid = insert_episode(con, ep, path, model, dim)
        report.episodes += 1
        for label in ep.nodes:
            nid = ensure_node(con, label, label_to_id, report)
            con.execute(
                "INSERT OR IGNORE INTO episode_nodes(episode_id, node_id) VALUES (?,?)",
                (eid, nid),
            )
            report.membrane += 1
        con.execute(
            "INSERT INTO episode_vectors(episode_id, embedding) VALUES (?, ?)",
            (eid, sqlite_vec.serialize_float32(vec)),
        )
        report.vectors += 1


def insert_episode(
    con: sqlite3.Connection, ep: Episode, path: Path, model: str, dim: int
) -> int:
    import json

    cur = con.execute(
        """
        INSERT INTO episodes(
            public_id, overview, summary, source_text, highlights_json, keywords_json,
            salience_tier, status, created_at, activated_at, last_accessed_at, archived_at,
            fragment_path, source_session_id, source_path,
            embedding_model, embedding_dim, last_embedded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ep.public_id,
            ep.overview,
            ep.summary,
            ep.source_text,
            json.dumps(ep.highlights, ensure_ascii=False) if ep.highlights else None,
            json.dumps(ep.keywords, ensure_ascii=False),
            ep.salience_tier,
            ep.status,
            ep.created_at,
            ep.activated_at,
            ep.activated_at,  # last_accessed_at 初值 = activated_at(§4)
            ep.archived_at,
            str(path),
            ep.source_session_id,
            ep.source_path,
            model,
            dim,
            _now(),
        ),
    )
    return cur.lastrowid


def rebuild(cfg: Config, provider: EmbeddingProvider, *, lock_meta: bool = True) -> RebuildReport:
    """从碎片全量重建 DB。DB 不存在则建库迁移;存在则清空内容后重灌。

    **fail-fast 原子**:所有可失败的工作(parse 碎片、联网重嵌)都在 `_clear` 之前
    做完;任一步抛错则 DB 内容原封不动,绝不留下半清空的库。

    lock_meta=False 时,若 meta 锁缺失则不落锁(用于 fake provider,避免把真实 DB 锁成 fake)。
    """
    con = connect(cfg.db_path)
    try:
        migrate.up(con)  # 删库后重建 schema;已存在则无操作
        report = RebuildReport()
        # ---- 阶段一:碰 DB 内容之前,把会失败的事全做完 ----
        nodes = load_all_nodes(cfg.nodes_dir)        # 坏 node 碎片在此 fail-fast
        eps = load_all_episodes(cfg.episodes_dir)    # 坏 episode 碎片在此 fail-fast
        model, dim = assert_embeddable(con, provider, lock_meta=lock_meta)  # 锁校验/落锁(不清内容)
        vectors = _embed_overviews(cfg, provider, eps, dim)  # 联网失败也在清库前抛
        # ---- 阶段二:全部成功,才动 DB 内容 ----
        _clear(con)
        label_to_id = _insert_nodes(con, nodes, report)
        _insert_episodes(con, eps, vectors, label_to_id, model, dim, report)
        con.commit()
        return report
    finally:
        con.close()
