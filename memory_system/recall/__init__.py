"""S6 检索层入口。

Phase 1 三路检索:细节(FTS grep)/ 情景(向量+FTS→RRF)/ 概念(膜 join),已齐。
重构封装在子模块 reconstruct(`from memory_system.recall import reconstruct` 后调 run)。
开场注入(S6-6 opening)由后续步骤补入,补入时在此追加导出——
__init__ 只导出已存在的东西,不放占位实现。
"""

from __future__ import annotations

from memory_system.recall.concept import recall_concept
from memory_system.recall.detail import recall_detail
from memory_system.recall.episode import recall_episode

__all__ = ["recall_concept", "recall_detail", "recall_episode"]
