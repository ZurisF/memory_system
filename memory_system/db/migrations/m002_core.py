"""002 core —— 情景 + 概念图 + 检索索引。

落 idea_v2 §6 的核心 schema(去掉 uuid 两表,推迟到 S2 跟扫描器一起设计):
  episodes(含 highlights_json + 粗粒度溯源列 source_session_id/source_path)
  nodes / node_aliases / episode_nodes(膜,FK CASCADE)
  episode_vectors(vec0,维度由 config 决定,与 meta 锁一致)
  episode_fts(trigram,外部内容表 content='episodes',触发器同步 source_text)

vec0 的 FLOAT[N] 必须在建表时定死 N。迁移本就不是纯 SQL(见 migrate 文档),
这里从 config 取 embedding.dim;init 写进 meta 的锁用的是同一个 cfg.embedding.dim,
构造上必然一致,之后所有写向量再被 meta 锁校验。
"""

from __future__ import annotations

import sqlite3


def up(con: sqlite3.Connection) -> None:
    from memory_system.config import load_config

    dim = load_config().embedding.dim

    # ---- 情景(核心表)----
    con.execute(
        """
        CREATE TABLE episodes (
            id                INTEGER PRIMARY KEY,
            public_id         TEXT NOT NULL UNIQUE,
            overview          TEXT NOT NULL,
            summary           TEXT NOT NULL,
            source_text       TEXT NOT NULL,
            highlights_json   TEXT,
            keywords_json     TEXT NOT NULL DEFAULT '[]',
            salience_tier     INTEGER NOT NULL DEFAULT 1
                              CHECK(salience_tier BETWEEN 1 AND 3),
            status            TEXT NOT NULL DEFAULT 'staging'
                              CHECK(status IN ('staging','active','rejected','archived')),
            created_at        TEXT NOT NULL,
            activated_at      TEXT,
            last_accessed_at  TEXT,
            archived_at       TEXT,
            fragment_path     TEXT NOT NULL UNIQUE,
            source_session_id TEXT,
            source_path       TEXT,
            embedding_model   TEXT,
            embedding_dim     INTEGER,
            last_embedded_at  TEXT
        )
        """
    )
    con.execute("CREATE INDEX idx_episodes_status ON episodes(status)")
    con.execute("CREATE INDEX idx_episodes_created ON episodes(created_at)")

    # ---- node + 别名 + 膜 ----
    con.execute(
        """
        CREATE TABLE nodes (
            id                   INTEGER PRIMARY KEY,
            label                TEXT NOT NULL UNIQUE,
            type                 TEXT,
            label_embedding_json TEXT,
            last_accessed_at     TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE node_aliases (
            alias   TEXT PRIMARY KEY,
            node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE episode_nodes (
            episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            node_id    INTEGER NOT NULL REFERENCES nodes(id)    ON DELETE CASCADE,
            PRIMARY KEY (episode_id, node_id)
        )
        """
    )
    con.execute("CREATE INDEX idx_episode_nodes_node ON episode_nodes(node_id)")

    # ---- 向量(vec0,维度与 meta 锁一致)----
    con.execute(
        f"""
        CREATE VIRTUAL TABLE episode_vectors USING vec0(
            episode_id INTEGER PRIMARY KEY,
            embedding  FLOAT[{dim}]
        )
        """
    )

    # ---- 全文(FTS5 trigram,外部内容表,触发器同步 source_text)----
    con.execute(
        """
        CREATE VIRTUAL TABLE episode_fts USING fts5(
            source_text,
            content='episodes',
            content_rowid='id',
            tokenize='trigram'
        )
        """
    )
    con.execute(
        """
        CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN
            INSERT INTO episode_fts(rowid, source_text) VALUES (new.id, new.source_text);
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN
            INSERT INTO episode_fts(episode_fts, rowid, source_text)
            VALUES ('delete', old.id, old.source_text);
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER episodes_au AFTER UPDATE ON episodes BEGIN
            INSERT INTO episode_fts(episode_fts, rowid, source_text)
            VALUES ('delete', old.id, old.source_text);
            INSERT INTO episode_fts(rowid, source_text) VALUES (new.id, new.source_text);
        END
        """
    )


def down(con: sqlite3.Connection) -> None:
    for trig in ("episodes_ai", "episodes_ad", "episodes_au"):
        con.execute(f"DROP TRIGGER IF EXISTS {trig}")
    for tbl in (
        "episode_fts",
        "episode_vectors",
        "episode_nodes",
        "node_aliases",
        "nodes",
        "episodes",
    ):
        con.execute(f"DROP TABLE IF EXISTS {tbl}")


from memory_system.db.migrate import Migration  # noqa: E402

MIGRATION = Migration(version=2, name="core", up=up, down=down)
