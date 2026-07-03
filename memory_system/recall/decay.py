"""惰性衰减:活跃度现算 + 时钟刷新机制(S6-1)。

设计(s6_build_plan §0.5 / §S6-1):
- 活跃度不落库,检索时按半衰期现算 `0.5 ** (elapsed_days / half_life_days[tier-1])`,
  改配置即全库生效、零迁移。
- `last_accessed_at` 是运行态时钟(命中刷新;index rebuild 重置为 activated_at)。
  为 NULL 时回退用 `activated_at`,再 NULL 用 `created_at`(防御老数据/未来数据)。
- 本模块只提供机制(现算 + UPDATE),**不 commit**——谁调用谁负责 commit
  (与全系统 DB 范式一致)。刷新规则(细节刷命中 / 情景刷 top-1+同源 / 概念只刷 node /
  开场全不刷)由各检索模块遵守,不写死在这里。

时钟 UPDATE 走内部整数 id(调用方查询时顺手带出;红线约束的是**对外输出**不带 id,
不是内部 DML)。effective_activation 的 cfg 形参是 `RecallConfig`(拿 half_life_days)。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable


def _parse_ts(s: str) -> datetime:
    # 碎片/DB 里的时间戳是 ISO 串;裸串无时区时按 UTC 处理(与 index._now 的 aware ISO 对齐)。
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _aware(now: datetime) -> datetime:
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def effective_activation(
    last_accessed_at: str | None,
    tier: int,
    cfg,
    now: datetime,
    *,
    activated_at: str | None = None,
    created_at: str | None = None,
) -> float:
    """现算活跃度 ∈ (0, 1]。半衰期公式,时间基准取 NULL 回退链首个非空值。

    signature 的位置参数固定为 `(last_accessed_at, tier, cfg, now)`(施工书契约);
    回退链的另两环用关键字参数传入,便于调用方直接把 episode 行的三列铺进来、也便于单测。
    三者全空(不该发生)→ 视作刚活跃(1.0),绝不抛异常拖垮检索。
    """
    ts = last_accessed_at or activated_at or created_at
    if not ts:
        return 1.0
    half = cfg.half_life_days[tier - 1]
    elapsed_days = (_aware(now) - _parse_ts(ts)).total_seconds() / 86400.0
    if elapsed_days < 0:  # 时钟偏移/未来时间戳:钳到 0,活跃度不越过 1.0
        elapsed_days = 0.0
    return 0.5 ** (elapsed_days / half)


def _iso(now: datetime) -> str:
    return _aware(now).isoformat() if isinstance(now, datetime) else str(now)


def touch_episodes(con: sqlite3.Connection, episode_ids: Iterable[int], now: datetime) -> None:
    """刷新一批 episode 的 `last_accessed_at`(主动命中)。只 UPDATE 不 commit。"""
    ids = list(episode_ids)
    if not ids:
        return
    ts = _iso(now)
    con.executemany("UPDATE episodes SET last_accessed_at=? WHERE id=?",
                    [(ts, i) for i in ids])


def touch_node(con: sqlite3.Connection, node_id: int, now: datetime) -> None:
    """刷新单个 node 的 `last_accessed_at`(概念命中)。只 UPDATE 不 commit。"""
    con.execute("UPDATE nodes SET last_accessed_at=? WHERE id=?", (_iso(now), node_id))
