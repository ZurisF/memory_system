"""003 processed —— 段级「已处理」flag + 会话级处理水位(S2 决定)。

去重粒度:段级,一段一行(非每消息一行)。覆盖的 message-uuid 集合作紧凑 JSON
挂在段行上;段哈希 = 排序后 uuid 集的 sha1 → 选中回合集的顺序无关稳定身份,幂等判重。

processed_segments:人工选段(及之后提取出的 episode)覆盖了哪些消息。
session_watermark:每会话处理到的 leaf uuid,标记「上次处理到哪」。

注:这是纯操作态(人工选段的编辑辅助书签),不是记忆正本,**不参与碎片重建**。
碎片里绝不含 uuid(三铁律),故删库 rebuild 后本表为空、已处理标记不恢复——这是
可接受代价:书签丢了顶多重看一段对话,记忆正本(碎片)完好。
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
