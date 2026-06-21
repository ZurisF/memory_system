"""段级「已处理」flag + 会话水位读写(配合 m003_processed)。

段哈希 = 排序后覆盖 uuid 集的 sha1:给「选中的回合集」一个与选择顺序无关的
稳定身份 → 同一组回合无论怎么点选、点几次,都判为同段,幂等去重。
(uuid 只圈在这张操作态书签表内:不进碎片、不上 UI、不做溯源。)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


def segment_hash(uuids: list[str]) -> str:
    joined = "\n".join(sorted(uuids))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProcessedSegment:
    segment_hash: str
    session_id: str
    covered_uuids: list[str]
    first_uuid: str | None
    last_uuid: str | None
    episode_public_id: str | None
    processed_at: str


def mark_segment(
    con: sqlite3.Connection,
    session_id: str,
    uuids: list[str],
    *,
    episode_public_id: str | None = None,
) -> str:
    """登记一段为已处理(幂等:同 uuid 集重复登记只更新 episode 链接/时间)。返回 segment_hash。"""
    if not uuids:
        raise ValueError("空 uuid 段不可登记")
    h = segment_hash(uuids)
    now = _now()
    con.execute(
        """
        INSERT INTO processed_segments(
            segment_hash, session_id, covered_uuids_json, first_uuid, last_uuid,
            episode_public_id, processed_at
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(segment_hash) DO UPDATE SET
            episode_public_id = COALESCE(excluded.episode_public_id, episode_public_id),
            processed_at = excluded.processed_at
        """,
        (h, session_id, json.dumps(uuids, ensure_ascii=False),
         uuids[0], uuids[-1], episode_public_id, now),
    )
    # 推进会话水位到本段末尾(简单策略:最近处理段的 last_uuid)
    con.execute(
        """
        INSERT INTO session_watermark(session_id, last_leaf_uuid, processed_at)
        VALUES (?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
            last_leaf_uuid = excluded.last_leaf_uuid,
            processed_at = excluded.processed_at
        """,
        (session_id, uuids[-1], now),
    )
    con.commit()
    return h


def is_processed(con: sqlite3.Connection, uuids: list[str]) -> bool:
    h = segment_hash(uuids)
    return con.execute(
        "SELECT 1 FROM processed_segments WHERE segment_hash=?", (h,)
    ).fetchone() is not None


def processed_uuids(con: sqlite3.Connection, session_id: str) -> set[str]:
    """某会话所有已处理段覆盖的 uuid 并集(供 UI 给回合打「已处理」标记)。"""
    out: set[str] = set()
    for (j,) in con.execute(
        "SELECT covered_uuids_json FROM processed_segments WHERE session_id=?", (session_id,)
    ).fetchall():
        out.update(json.loads(j))
    return out


def get_watermark(con: sqlite3.Connection, session_id: str) -> str | None:
    row = con.execute(
        "SELECT last_leaf_uuid FROM session_watermark WHERE session_id=?", (session_id,)
    ).fetchone()
    return row[0] if row else None


def list_segments(con: sqlite3.Connection, session_id: str) -> list[ProcessedSegment]:
    rows = con.execute(
        "SELECT segment_hash, session_id, covered_uuids_json, first_uuid, last_uuid, "
        "episode_public_id, processed_at FROM processed_segments WHERE session_id=? "
        "ORDER BY processed_at",
        (session_id,),
    ).fetchall()
    return [
        ProcessedSegment(
            segment_hash=r[0], session_id=r[1], covered_uuids=json.loads(r[2]),
            first_uuid=r[3], last_uuid=r[4], episode_public_id=r[5], processed_at=r[6],
        )
        for r in rows
    ]
