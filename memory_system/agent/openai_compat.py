"""OpenAI 兼容 chat provider —— DeepSeek / qwen 等,urllib 直连(不引 openai)。

base_url / api_key_env 来自 AgentConfig;key 从环境读。POST {base_url}/chat/completions,
messages=[system, user],非流式,取 choices[0].message.content。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from memory_system.agent.base import ChatError, ChatProvider, ChatResult, ChatTimeout
from memory_system.agent.registry import PLACEHOLDER_KEY as _PLACEHOLDER_KEY


class OpenAICompatProvider(ChatProvider):
    id = "openai_compat"

    def __init__(self, base_url: str, api_key_env: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env

    def available(self) -> tuple[bool, str]:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            return False, f"环境变量 {self.api_key_env} 未设置"
        if key == _PLACEHOLDER_KEY:
            return False, f"环境变量 {self.api_key_env} 仍是占位 key"
        return True, self.base_url

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ChatError(f"环境变量 {self.api_key_env} 未设置;key 只从环境读,不落盘。")
        if key == _PLACEHOLDER_KEY:
            raise ChatError(f"环境变量 {self.api_key_env} 仍是占位 key;请先替换为真实 key。")
        return key

    def complete(self, system: str, user: str, *, model: str, timeout: int) -> ChatResult:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except TimeoutError as e:
            raise ChatTimeout(f"{self.base_url} 超时(>{timeout}s)") from e
        except urllib.error.HTTPError as e:
            raise ChatError(f"HTTP {e.code}: {e.read().decode()[:500]}") from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, TimeoutError):
                raise ChatTimeout(f"{self.base_url} 超时(>{timeout}s)") from e
            raise ChatError(f"网络错误: {reason}") from e

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ChatError(f"响应缺 choices[0].message.content: {str(data)[:300]}") from e
        if not isinstance(text, str) or not text.strip():
            raise ChatError("响应 content 为空")

        return ChatResult(
            text=text,
            model=data.get("model", model),
            usage=data.get("usage") or {},
            cost_usd=None,
            duration_ms=None,
            raw=data,
        )
