"""S6 检索层入口。

三路检索:细节(FTS grep)/ 情景(向量+FTS→RRF)/ 概念(膜 join)。
重构封装在子模块 reconstruct(`from memory_system.recall import reconstruct` 后调 run);
开场注入封装在子模块 opening(`from memory_system.recall import opening`)。
__init__ 只导出三路函数,子模块按需导入。
"""

from __future__ import annotations

from memory_system.recall.concept import recall_concept
from memory_system.recall.detail import recall_detail
from memory_system.recall.episode import recall_episode

__all__ = ["recall_concept", "recall_detail", "recall_episode"]
