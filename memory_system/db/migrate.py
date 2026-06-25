"""迁移器:有序迁移列表,支持 up / down / status。

每个迁移是一个对象,带 version:int、name:str、up(con)、down(con)。
迁移器自管 schema_migrations 记账表。vec0/fts5 这类要扩展的建表也能在 up() 里跑,
所以迁移用 Python 函数而非纯 .sql。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None]


def _ensure_bookkeeping(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def all_migrations() -> list[Migration]:
    """从 migrations 包收集,按 version 升序。"""
    from memory_system.db import migrations as _migrations_pkg  # 惰性导入,避开循环

    migs = list(_migrations_pkg.REGISTRY)
    migs.sort(key=lambda m: m.version)
    # 防呆:版本号不许重复
    seen = set()
    for m in migs:
        if m.version in seen:
            raise ValueError(f"重复迁移版本号: {m.version}")
        seen.add(m.version)
    return migs


def applied_versions(con: sqlite3.Connection) -> set[int]:
    """返回 schema_migrations 里实际存在的版本集合(不靠 MAX 推断)。"""
    _ensure_bookkeeping(con)
    rows = con.execute("SELECT version FROM schema_migrations").fetchall()
    return {r["version"] for r in rows}


def current_version(con: sqlite3.Connection) -> int:
    """已应用的最大版本号;无迁移记录则 0。"""
    vers = applied_versions(con)
    return max(vers) if vers else 0


def status(con: sqlite3.Connection) -> list[tuple[int, str, bool]]:
    """返回 [(version, name, applied?)],按版本升序。"""
    applied = applied_versions(con)
    return [(m.version, m.name, m.version in applied) for m in all_migrations()]


def up(con: sqlite3.Connection, *, target: int | None = None) -> list[int]:
    """应用所有未应用且 version <= target 的迁移。返回新应用的版本列表。"""
    from datetime import datetime, timezone

    _ensure_bookkeeping(con)
    applied = applied_versions(con)
    new_applied: list[int] = []
    for m in all_migrations():
        if m.version in applied:
            continue
        if target is not None and m.version > target:
            break
        m.up(con)
        con.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (m.version, m.name, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        new_applied.append(m.version)
    return new_applied


def down(con: sqlite3.Connection, *, steps: int = 1) -> list[int]:
    """回滚最近 steps 个迁移。返回被回滚的版本列表。"""
    _ensure_bookkeeping(con)
    by_version = {m.version: m for m in all_migrations()}
    rolled: list[int] = []
    applied_desc = [
        r["version"]
        for r in con.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC"
        ).fetchall()
    ]
    for version in applied_desc[:steps]:
        m = by_version.get(version)
        if m is None:
            raise ValueError(f"已应用的版本 {version} 在代码里找不到迁移定义,无法回滚")
        m.down(con)
        con.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
        con.commit()
        rolled.append(version)
    return rolled
