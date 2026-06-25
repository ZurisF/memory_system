"""chat agent provider:统一接口 + 工厂 + JSON 提取(剥围栏、定位平衡 {})。"""

from __future__ import annotations

import json

from memory_system.agent.base import (
    ChatError,
    ChatProvider,
    ChatResult,
    ChatTimeout,
)
from memory_system.config import AgentConfig


def get_chat_provider(cfg: AgentConfig) -> ChatProvider:
    if cfg.provider == "fake":
        from memory_system.agent.fake import FakeChatProvider

        return FakeChatProvider()
    if cfg.provider == "claude_cli":
        from memory_system.agent.claude_cli import ClaudeCliProvider

        return ClaudeCliProvider()
    if cfg.provider in ("deepseek", "openai_compat", "qwen"):
        from memory_system.agent.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(cfg.base_url or "https://api.deepseek.com/v1",
                                    cfg.api_key_env or "DEEPSEEK_API_KEY")
    # 自定义 provider(控制台动态添加的 OpenAI 兼容端点)
    cp = cfg.custom_providers.get(cfg.provider)
    if cp:
        from memory_system.agent.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(cp["base_url"], cp["api_key_env"])
    raise ValueError(f"未知 agent provider: {cfg.provider!r}")


def _balanced_object(s: str) -> str | None:
    """返回 s 中第一个平衡的 {...} 子串(尊重字符串与转义),无则 None。"""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON 对象:先剥 ```json 围栏,再定位首个平衡 {}。

    解析失败抛 ValueError(上层视作坏响应,可重试)。
    """
    s = text.strip()
    # 剥代码围栏:```json … ``` 或 ``` … ```
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    # 直接整体解析
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 退而求其次:抠第一个平衡对象
    cand = _balanced_object(s)
    if cand is not None:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            raise ValueError(f"提取到的 JSON 仍解析失败: {e}") from e
    raise ValueError(f"输出里找不到 JSON 对象: {text[:200]!r}")


__all__ = [
    "ChatProvider",
    "ChatResult",
    "ChatError",
    "ChatTimeout",
    "get_chat_provider",
    "extract_json",
]
