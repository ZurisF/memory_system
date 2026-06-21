"""离线确定性 chat provider —— 无网络/无 key 跑通切块链路与测试。

不真调模型:按"行为脚本"逐次产出。verify_s3 用它注入:好分段、坏 JSON、超时、
围栏包裹、首败后成(测重试)。脚本耗尽后落到 default_responder / 内置自动分段。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from memory_system.agent.base import ChatProvider, ChatResult

_TURN = re.compile(r"【回合\s*(\d+)】")


def make_segments(segs: list[dict]) -> str:
    """把段列表序列化成切块 agent 约定的 JSON 文本。"""
    return json.dumps({"segments": segs}, ensure_ascii=False)


def auto_segment(user: str) -> str:
    """从对话文本里读最大回合号,产出确定性分段:>=4 回合切两段,否则单段。"""
    nums = [int(m) for m in _TURN.findall(user)]
    last = max(nums) if nums else 1
    if last >= 4:
        mid = last // 2
        segs = [
            {"start": 1, "end": mid, "tag": "前半",
             "cut_reason": "渐变漂移:此处为最弱弯折", "short": False, "deletions": []},
            {"start": mid + 1, "end": last, "tag": "后半",
             "cut_reason": "阶段转换", "short": False, "deletions": []},
        ]
    else:
        segs = [{"start": 1, "end": last, "tag": "全篇",
                 "cut_reason": "单弧线:全篇为一次完整的情绪和思辨运动,无自然闭合点",
                 "short": last < 4, "deletions": []}]
    return make_segments(segs)


class FakeChatProvider(ChatProvider):
    id = "fake"

    def __init__(
        self,
        behaviors: list | None = None,
        default_responder: Callable[[str, str, str], str] | None = None,
    ) -> None:
        # behaviors 每项:str(直接返回)/ Exception 实例(抛出)/ callable(system,user,model)->str
        self._behaviors = list(behaviors or [])
        self._default = default_responder
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return True, "fake(离线)"

    def complete(self, system: str, user: str, *, model: str, timeout: int) -> ChatResult:
        self.calls += 1
        if self._behaviors:
            b = self._behaviors.pop(0)
            if isinstance(b, Exception):
                raise b
            text = b(system, user, model) if callable(b) else b
        elif self._default is not None:
            text = self._default(system, user, model)
        else:
            text = auto_segment(user)
        return ChatResult(
            text=text, model=model or "fake",
            usage={"input_tokens": len(user), "output_tokens": len(text)},
            cost_usd=0.0, duration_ms=0,
        )
