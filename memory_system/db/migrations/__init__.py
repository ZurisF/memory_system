"""迁移注册表。

S0:001 meta(锁 embedding 模型/维度 + schema 版本记账)。
S1:002 core(episodes + nodes/aliases/膜 + sources/processed + vectors + fts)。
"""

from memory_system.db.migrations import m001_meta, m002_core, m003_processed

REGISTRY = [
    m001_meta.MIGRATION,
    m002_core.MIGRATION,
    m003_processed.MIGRATION,
]
