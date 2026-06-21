"""001 meta —— 锁 embedding 模型/维度,schema 版本由迁移器记账。

meta 是个简单 key/value 表。写向量前要校验 embedding_model/embedding_dim 与此处一致,
不一致拒写(不补零不截断)。换模型 → 从碎片全量重嵌。
"""

from __future__ import annotations

import sqlite3


def up(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    # 初值留空,由 init 用当前 config 写入(见 cli.init),避免迁移硬编码维度。


def down(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS meta")


# 在 migrate 注册表引用
from memory_system.db.migrate import Migration  # noqa: E402

MIGRATION = Migration(version=1, name="meta", up=up, down=down)
