"""004 injected_log —— 检索注入台账(S6 Phase 2:session 去重 / 跨 session 冷却)。

记录「哪个 session 在何时被注入过哪条 episode」,支撑两件事(裁定 §0.2/§0.3):
  - 去重(硬):同 session 已注入的 public_id 从**全部三槽**候选硬排除(已在对方上下文里,再给是浪费)。
  - 冷却(软):其他 session 在窗口内注入过的候选 RRF×衰减分乘 cooldown_factor(温和降序,不排除)。

存 public_id 不存整数 id(裁定 §0.1):index rebuild 会重排整数 id,存整数即跨 rebuild 腐坏;
public_id 是稳定身份(与红线一致)。故 **rebuild 不清这张表**——它记的是「注入过什么」,
与索引重建无关,public_id 稳定所以跨 rebuild 依然有效。

tool 列本轮恒填 'episode'(去重/冷却只作用于 episode 检索);留列给将来 MCP 阶段填真实 tool 名,
为跨 tool 融合备数据。纯运行态台账,不是记忆正本、不参与碎片重建(与 processed 系同类)。
"""

from __future__ import annotations

import sqlite3


def up(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE injected_log (
            id          INTEGER PRIMARY KEY,
            session_key TEXT NOT NULL,
            public_id   TEXT NOT NULL,
            tool        TEXT NOT NULL,
            hit_at      TEXT NOT NULL
        )
        """
    )
    # 同 session 去重:按 (session_key, public_id) 查该 session 注入过什么。
    con.execute("CREATE INDEX idx_injected_session ON injected_log(session_key, public_id)")
    # 跨 session 冷却:按 (public_id, hit_at) 查某条在时间窗内是否被别的 session 注入过。
    con.execute("CREATE INDEX idx_injected_public ON injected_log(public_id, hit_at)")


def down(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS injected_log")


from memory_system.db.migrate import Migration  # noqa: E402

MIGRATION = Migration(version=4, name="injected_log", up=up, down=down)
