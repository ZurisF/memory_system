"""chat agent provider 接口 —— 切块/提取/重构共用的"系统提示 + 用户输入 → 文本"变换。

与 embedding 分开:embedding 是 text→vector,agent 是 (system,user)→text。
provider 自报能力,complete() 返回文本 + 记账信息(usage/cost/duration),
超时或后端报错抛 ChatError(上层据此重试 / 进 retry 列表)。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


class ChatError(RuntimeError):
    """后端调用失败(网络、超时、非零退出、HTTP 错误、坏响应)。"""


class ChatTimeout(ChatError):
    """单次调用超时(单独分型,便于上层针对超时给出"重试"提示)。"""


@dataclass
class ChatResult:
    text: str                       # 模型输出正文(已从信封/响应里取出,未做 JSON 解析)
    model: str
    usage: dict = field(default_factory=dict)   # {input_tokens, output_tokens, ...}
    cost_usd: float | None = None
    duration_ms: int | None = None
    raw: dict | None = None         # 原始信封/响应,排障用


class ChatProvider(abc.ABC):
    """provider 自报 id;complete() 做一次 (system,user)→text。"""

    id: str = "base"

    @abc.abstractmethod
    def complete(self, system: str, user: str, *, model: str, timeout: int) -> ChatResult:
        """跑一次补全。失败抛 ChatError;超时抛 ChatTimeout。"""

    def available(self) -> tuple[bool, str]:
        """该 provider 当前能否用(在 PATH / key 已设)。返回 (可用, 原因/说明)。"""
        return True, ""
