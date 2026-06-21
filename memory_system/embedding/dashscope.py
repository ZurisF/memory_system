"""DashScope text-embedding-v4,OpenAI 兼容口,标准库 urllib 直连(不引 openai)。

- base_url / model / dim 来自 config;key 从环境变量读。
- 批量有上限(约 10/请求),embed() 自动分批。
- 维度显式带上(dimensions),并校验返回长度。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from memory_system.config import EmbeddingConfig
from memory_system.embedding.base import EmbeddingProvider


class EmbeddingError(RuntimeError):
    pass


class DashScopeProvider(EmbeddingProvider):
    def __init__(self, cfg: EmbeddingConfig) -> None:
        super().__init__(model=cfg.model, dim=cfg.dim)
        self.base_url = cfg.base_url.rstrip("/")
        self.api_key_env = cfg.api_key_env
        self.batch_size = cfg.batch_size

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise EmbeddingError(
                f"环境变量 {self.api_key_env} 未设置;embedding key 只从环境读,不落盘。"
            )
        return key

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._embed_batch(texts[i : i + self.batch_size]))
        self._check_dims(out)
        return out

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": batch, "dimensions": self.dim}
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise EmbeddingError(f"HTTP {e.code}: {e.read().decode()[:500]}") from e
        except urllib.error.URLError as e:
            raise EmbeddingError(f"网络错误: {e.reason}") from e

        # OpenAI 兼容:data 按 index 排序后取 embedding
        items = sorted(data["data"], key=lambda d: d["index"])
        return [it["embedding"] for it in items]
