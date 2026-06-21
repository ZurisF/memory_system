"""003 processed —— 段级「已处理」flag + 会话级 resume 水位(S2 决定)。

去重粒度:段级,一段一行(非每消息一行)。覆盖的 message-uuid 集合作紧凑 JSON
挂在段行上;段哈希 = 排序后 uuid 集的 sha1 → resume 复刻保留 uuid,同段同哈希,幂等。

processed_segments:人工选段(及之后提取出的 episode)覆盖了哪些消息。
session_watermark:每会话处理到的 leaf uuid,供 resume 续点判断。

注:这是操作态(人工选段的书签),不是记忆正本。episode 落地后其碎片会自带
covered_uuids,届时可由 index rebuild 回填 episode-backed 的段;纯选未提取的段不参与重建。
"""

from __future__ import annotations

import sqlite3


def up(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE processed_segments (
            segment_hash      TEXT PRIMARY KEY,
            session_id        TEXT NOT NULL,
            covered_uuids_json TEXT NOT NULL,
            first_uuid        TEXT,
            last_uuid         TEXT,
            episode_public_id TEXT,
            processed_at      TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX idx_proc_seg_session ON processed_segments(session_id)")
    con.execute(
        """
        CREATE TABLE session_watermark (
            session_id     TEXT PRIMARY KEY,
            last_leaf_uuid TEXT,
            processed_at   TEXT NOT NULL
        )
        """
    )


def down(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS session_watermark")
    con.execute("DROP TABLE IF EXISTS processed_segments")


from memory_system.db.migrate import Migration  # noqa: E402

MIGRATION = Migration(version=3, name="processed", up=up, down=down)
