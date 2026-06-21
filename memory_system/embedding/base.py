"""embedding provider 接口。

约定:embed() 接受一批文本,返回等长的向量列表;每个向量长度 == dim。
provider 自报 model 与 dim,写向量前由上层校验与 meta 一致。
"""

from __future__ import annotations

import abc


class EmbeddingProvider(abc.ABC):
    def __init__(self, model: str, dim: int) -> None:
        self.model = model
        self.dim = dim

    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本嵌成向量。实现需保证返回长度 == len(texts),每条长度 == dim。"""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def _check_dims(self, vectors: list[list[float]]) -> None:
        for i, v in enumerate(vectors):
            if len(v) != self.dim:
                raise ValueError(
                    f"provider {self.model} 返回第 {i} 条维度 {len(v)} ≠ 期望 {self.dim}"
                )
