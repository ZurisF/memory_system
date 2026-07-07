"""迁移注册表。

S0:001 meta(锁 embedding 模型/维度 + schema 版本记账)。
S1:002 core(episodes + nodes/aliases/膜 + sources/processed + vectors + fts)。
S2:003 processed(段级已处理 flag + 会话处理水位)。
S6-P2:004 injected_log(检索注入台账:session 去重 / 跨 session 冷却)。
"""

from memory_system.db.migrations import (
    m001_meta,
    m002_core,
    m003_processed,
    m004_injected_log,
)

REGISTRY = [
    m001_meta.MIGRATION,
    m002_core.MIGRATION,
    m003_processed.MIGRATION,
    m004_injected_log.MIGRATION,
]
