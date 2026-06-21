"""embedding provider:统一接口 + fake(离线确定性) + dashscope(urllib 直连)。"""

from __future__ import annotations

from memory_system.config import EmbeddingConfig
from memory_system.embedding.base import EmbeddingProvider
from memory_system.embedding.dashscope import DashScopeProvider
from memory_system.embedding.fake import FakeProvider


def get_provider(cfg: EmbeddingConfig) -> EmbeddingProvider:
    if cfg.provider == "fake":
        return FakeProvider(model="fake", dim=cfg.dim)
    if cfg.provider == "dashscope":
        return DashScopeProvider(cfg)
    raise ValueError(f"未知 embedding provider: {cfg.provider!r}")


__all__ = ["EmbeddingProvider", "FakeProvider", "DashScopeProvider", "get_provider"]
