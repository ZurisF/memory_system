"""claude_cli provider —— 本机 `claude -p` 头无模式当纯变换(复用订阅,不烧 key)。

实测(2026-06-20, claude 2.1.179)要点:
  --system-prompt 全覆盖默认系统提示;--exclude-dynamic-system-prompt-sections
  再剥掉 CLAUDE.md/记忆注入 ⇒ 干净变换。--disallowed-tools "*" 禁所有工具。
  --output-format json 给信封,result 字段即模型输出,另带 usage/total_cost_usd/duration_ms。
  坑:不喂 stdin 会干等 3 秒 ⇒ stdin=DEVNULL;模型偶尔给 ```json 围栏 ⇒ 上层 extract_json。
"""

from __future__ import annotations

import json
import shutil
import subprocess

from memory_system.agent.base import ChatError, ChatProvider, ChatResult, ChatTimeout


class ClaudeCliProvider(ChatProvider):
    id = "claude_cli"

    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.binary)
        if not path:
            return False, f"{self.binary} 不在 PATH"
        return True, path

    def complete(self, system: str, user: str, *, model: str, timeout: int) -> ChatResult:
        exe = shutil.which(self.binary)
        if not exe:
            raise ChatError(f"{self.binary} 不在 PATH,无法用 claude_cli provider")
        cmd = [
            exe, "-p", user,
            "--system-prompt", system,
            "--output-format", "json",
            "--disallowed-tools", "*",
            "--exclude-dynamic-system-prompt-sections",
        ]
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,   # 不喂 stdin,否则干等 3 秒
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ChatTimeout(f"claude -p 超时(>{timeout}s)") from e
        except OSError as e:
            raise ChatError(f"claude -p 启动失败: {e}") from e

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise ChatError(f"claude -p 退出码 {proc.returncode}: {tail}")

        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ChatError(f"claude -p 输出非 JSON 信封: {proc.stdout[:300]!r}") from e

        if env.get("is_error") or env.get("subtype") != "success":
            raise ChatError(
                f"claude -p 报错: subtype={env.get('subtype')} "
                f"api_error={env.get('api_error_status')}"
            )

        text = env.get("result")
        if not isinstance(text, str) or not text.strip():
            raise ChatError("claude -p 信封缺 result 文本")

        usage = env.get("usage") or {}
        # 实际用的模型(信封 modelUsage 的 key),回退到请求 model
        used_model = next(iter((env.get("modelUsage") or {}).keys()), model or "claude")
        return ChatResult(
            text=text,
            model=used_model,
            usage=usage,
            cost_usd=env.get("total_cost_usd"),
            duration_ms=env.get("duration_ms"),
            raw=env,
        )
