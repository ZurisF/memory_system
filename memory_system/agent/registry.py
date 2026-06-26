"""provider 注册表 —— 所有 provider 知识的单一来源。

把过去散在 config.py / agent/__init__.py / server.py 三处的 provider 知识收成一处:

- 内置 provider 目录(id / 显示名 / 类型 / 默认 base_url / 默认 key 环境变量);
- 自定义 provider(控制台动态添加的 OpenAI 兼容端点)的加载 / 保存 / 派生映射;
- id 合集、可用性拼装、各 provider 的 key 状态、key 掩码、占位 key 常量。

工厂(agent/__init__.py)、配置(config.py)、HTTP 层(server.py)都从这里读,不再各自硬编码。
凡需要 Config 的函数都按鸭子类型用 cfg.agent / cfg.home,不在模块顶层 import Config,避免
与 config.py 的循环依赖(config.load_config 反过来调本模块的 custom_map)。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

# 控制台添加 provider 时写入 .env 的占位 key;真实 key 永远从环境读、绝不落盘。
# 历史上这个字面量在 server.py / openai_compat.py 各写一份,现统一到此。
PLACEHOLDER_KEY = "[this is your api key]"


@dataclass(frozen=True)
class BuiltinProvider:
    id: str
    name: str            # 控制台显示名
    kind: str            # claude_cli | openai_compat | fake —— 决定工厂走哪个 provider 类
    base_url: str = ""   # openai_compat 家族在 cfg 未给 base_url 时的兜底默认
    key_env: str = ""    # 需要 key 的 provider 的环境变量名;无 key 的留空


# 内置 provider 目录(顺序即前端选择器/key 状态列表的展示顺序)。
# deepseek / openai_compat / qwen 共用运行时的 AgentConfig.base_url / api_key_env
# (默认指向 deepseek);这里给的 base_url/key_env 只是工厂的兜底默认。
BUILTINS: list[BuiltinProvider] = [
    BuiltinProvider("claude_cli", "Claude CLI", "claude_cli"),
    BuiltinProvider("deepseek", "DeepSeek", "openai_compat",
                    "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    BuiltinProvider("openai_compat", "OpenAI 兼容", "openai_compat",
                    "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    BuiltinProvider("qwen", "通义千问", "openai_compat",
                    "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    BuiltinProvider("fake", "Fake(离线测试)", "fake"),
]

BUILTIN_IDS: list[str] = [b.id for b in BUILTINS]
_BUILTIN_MAP: dict[str, BuiltinProvider] = {b.id: b for b in BUILTINS}


def builtin(pid: str) -> BuiltinProvider | None:
    return _BUILTIN_MAP.get(pid)


def is_builtin(pid: str) -> bool:
    return pid in _BUILTIN_MAP


# ---- key 掩码 ----
def mask_key(env_var: str) -> str | None:
    """读环境变量,返回掩码形式如 sk-****abcd;未配返回 None。"""
    v = os.environ.get(env_var, "").strip()
    if not v:
        return None
    if len(v) <= 8:
        return v[:2] + "****" + v[-2:]
    return v[:3] + "****" + v[-4:]


# ---- 自定义 provider 持久化(custom_providers.json) ----
def _custom_path(home: Path) -> Path:
    return home / "custom_providers.json"


def load_custom(home: Path) -> list[dict]:
    """加载用户通过控制台添加的自定义 provider(list 形态);文件不存在返回 []。"""
    p = _custom_path(home)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
        return data.get("providers", []) if isinstance(data, dict) else []
    except (ValueError, KeyError):
        return []


def save_custom(home: Path, providers: list[dict]) -> None:
    """保存自定义 provider 列表到 JSON 文件。"""
    p = _custom_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"providers": providers}, ensure_ascii=False, indent=2), "utf-8")


def custom_map(home: Path) -> dict:
    """从 list 形态派生 AgentConfig 用的 {id: {base_url, api_key_env, default_model}} 映射。"""
    return {cp["id"]: {"base_url": cp["base_url"], "api_key_env": cp["api_key_env"],
                       "default_model": cp.get("default_model", "")}
            for cp in load_custom(home)}


# ---- 合集 / 可用性 / key 状态 ----
def all_provider_ids(cfg) -> list[str]:
    """内置 + 自定义 provider id 合集。"""
    return BUILTIN_IDS + [cp["id"] for cp in load_custom(cfg.home)]


def providers_info(cfg) -> list[dict]:
    """各 provider 的可用性 + 显示信息(供前端选择器)。占位 key 视作不可用。"""
    from memory_system.agent import get_chat_provider

    out: list[dict] = []
    for b in BUILTINS:
        try:
            prov = get_chat_provider(replace(cfg.agent, provider=b.id))
            ok, why = prov.available()
            key_env = getattr(prov, "api_key_env", None)
            if key_env and os.environ.get(key_env, "").strip() == PLACEHOLDER_KEY:
                ok, why = False, f"环境变量 {key_env} 仍是占位 key"
        except Exception as e:  # noqa: BLE001
            ok, why = False, str(e)
        out.append({"id": b.id, "available": ok, "reason": why,
                    "default": b.id == cfg.agent.provider, "builtin": True})

    for cp in load_custom(cfg.home):
        pid = cp["id"]
        try:
            prov = get_chat_provider(replace(cfg.agent,
                provider=pid, custom_providers={pid: cp}))
            ok, why = prov.available()
            if cp.get("api_key_env") and os.environ.get(cp["api_key_env"], "").strip() == PLACEHOLDER_KEY:
                ok, why = False, f"环境变量 {cp['api_key_env']} 仍是占位 key"
        except Exception as e:  # noqa: BLE001
            ok, why = False, str(e)
        out.append({"id": pid, "available": ok, "reason": why,
                    "default": pid == cfg.agent.provider, "builtin": False,
                    "name": cp.get("name", pid), "base_url": cp.get("base_url", ""),
                    "default_model": cp.get("default_model", "")})
    return out


def agent_key_status(cfg) -> list[dict]:
    """各 agent provider 的 key 状态。

    claude_cli / fake 不走 key(三项均 None);openai_compat 家族共用运行时的
    cfg.agent.api_key_env;自定义 provider 各用自己的 api_key_env。
    """
    out: list[dict] = []
    for b in BUILTINS:
        if b.kind == "openai_compat":
            env_var = cfg.agent.api_key_env
            v = os.environ.get(env_var, "").strip()
            out.append({"id": b.id, "key_env": env_var,
                        "key_present": bool(v and v != PLACEHOLDER_KEY),
                        "key_masked": mask_key(env_var)})
        else:
            out.append({"id": b.id, "key_env": None, "key_present": None, "key_masked": None})
    for cp in load_custom(cfg.home):
        v = os.environ.get(cp["api_key_env"], "").strip()
        out.append({"id": cp["id"], "key_env": cp["api_key_env"],
                    "key_present": bool(v and v != PLACEHOLDER_KEY),
                    "key_masked": mask_key(cp["api_key_env"])})
    return out
