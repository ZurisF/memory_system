"""离线确定性假向量:同一文本永远得同一向量,单位长度。

用于无网络/无 key 时跑通检索链路与测试。不要写进真库(model='fake' 会被 meta 校验挡下)。
"""

from __future__ import annotations

import hashlib
import math

from memory_system.embedding.base import EmbeddingProvider


class FakeProvider(EmbeddingProvider):
    def embed(self, texts: list[str]) -> list[list[float]]:
        out = [self._one(t) for t in texts]
        self._check_dims(out)
        return out

    def _one(self, text: str) -> list[float]:
        # 用 sha256 链式扩展出足够字节,映射到 [-1,1],再归一化。
        buf = bytearray()
        seed = text.encode("utf-8")
        counter = 0
        while len(buf) < self.dim * 2:
            h = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            buf.extend(h)
            counter += 1
        vals = [
            (int.from_bytes(buf[i * 2 : i * 2 + 2], "big") / 65535.0) * 2.0 - 1.0
            for i in range(self.dim)
        ]
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        return [v / norm for v in vals]
