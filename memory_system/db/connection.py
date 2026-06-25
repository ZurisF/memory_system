"""打开 SQLite 连接并加载 sqlite-vec。

约定:外键开、WAL 模式;vec0 扩展在每个连接加载一次。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec


def connect(db_path: Path, *, load_vec: bool = True) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    if load_vec:
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
    return con


def vec_version(con: sqlite3.Connection) -> str:
    (ver,) = con.execute("SELECT vec_version()").fetchone()
    return ver
