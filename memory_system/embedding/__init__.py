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


def probe(cfg: EmbeddingConfig) -> tuple[bool, str, int | None]:
    """对 embedding 端点做一次最小探活:嵌单个短词,验证连通性与维度。

    返回 (ok, detail, dim)。不抛异常——失败折成 (False, 原因, None) 交上层编排。
    fake provider 不联网,直接报可用。
    """
    try:
        prov = get_provider(cfg)
        if cfg.provider == "fake":
            return True, "fake embedding 始终可用", cfg.dim
        vec = prov.embed_one("test")
        if not vec or not isinstance(vec, list):
            return False, "返回空向量", None
        return True, f"嵌入成功,维度={len(vec)},模型={cfg.model}", len(vec)
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300], None


__all__ = ["EmbeddingProvider", "FakeProvider", "DashScopeProvider", "get_provider", "probe"]
