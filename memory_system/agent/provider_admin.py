"""Agent provider administration and process-local runtime settings.

``Config`` remains the immutable startup snapshot.  The Web server registers that
snapshot here and all console-driven provider/model changes replace only the
process-local ``AgentConfig`` held by this module.  Persistence still follows the
existing order: add writes ``.env`` before the custom-provider catalog, while
remove writes the catalog before clearing dangling role overrides in ``.env``.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import datetime, timezone

from memory_system.agent import registry
from memory_system.config import AgentConfig, Config
from memory_system.env import load_dotenv, update_dotenv


class ProviderAdminError(Exception):
    """Invalid provider administration request."""


class ProviderForbiddenError(ProviderAdminError):
    """The requested mutation targets a protected provider."""


class ProviderNotFoundError(ProviderAdminError):
    """The requested custom provider does not exist."""


class ProviderConflictError(ProviderAdminError):
    """The requested provider id already exists."""


class ProviderPersistenceError(ProviderAdminError):
    """Provider settings could not be persisted."""


# Config contains a dict and is intentionally unhashable.  Keep its identity next
# to the current AgentConfig so a recycled id can never select another snapshot.
_runtime_agents: dict[int, tuple[Config, AgentConfig]] = {}


def initialize_runtime(cfg: Config) -> None:
    """Register ``cfg.agent`` as the initial runtime value for one server instance."""
    with registry.CUSTOM_LOCK:
        _runtime_agents[id(cfg)] = (cfg, cfg.agent)


def current_agent(cfg: Config) -> AgentConfig:
    """Return the current process-local agent settings for ``cfg``."""
    with registry.CUSTOM_LOCK:
        entry = _runtime_agents.get(id(cfg))
        if entry is None or entry[0] is not cfg:
            entry = (cfg, cfg.agent)
            _runtime_agents[id(cfg)] = entry
        return entry[1]


def runtime_config(cfg: Config) -> Config:
    """Return a Config view with current agent settings; never mutate ``cfg``."""
    return replace(cfg, agent=current_agent(cfg))


def _set_current_agent(cfg: Config, agent: AgentConfig) -> None:
    _runtime_agents[id(cfg)] = (cfg, agent)


def get_provider_listing(cfg: Config) -> dict:
    """Build the legacy ``/api/agent/providers`` payload from current settings."""
    active = runtime_config(cfg)
    return {
        "providers": registry.providers_info(active),
        "chunk_model": active.agent.chunk_model,
        "extract_model": active.agent.extract_model,
        "chunk_provider": active.agent.provider_for("chunk"),
        "extract_provider": active.agent.provider_for("extract"),
    }


def get_agent_settings(cfg: Config) -> dict:
    """Return the transport-neutral agent-console settings view.

    Reloading dotenv here refreshes key status after manual edits without letting
    a dotenv-owned placeholder overwrite a key exported by the parent shell.
    """
    load_dotenv(cfg.home / ".env")
    with registry.CUSTOM_LOCK:
        agent = current_agent(cfg)
        custom = registry.custom_map(cfg.home)
        if custom != agent.custom_providers:
            agent = replace(agent, custom_providers=custom)
            _set_current_agent(cfg, agent)

    active = replace(cfg, agent=agent)
    providers = registry.providers_info(active)
    agents = {
        "chunk": {
            "provider": agent.provider_for("chunk"),
            "model": agent.chunk_model,
            "providers": providers,
        },
        "extract": {
            "provider": agent.provider_for("extract"),
            "model": agent.extract_model,
            "providers": providers,
        },
        # Empty recall_provider deliberately means "follow global" and is not
        # resolved here; the console needs to display that override state.
        "recall": {
            "provider": agent.recall_provider,
            "model": agent.recall_model,
            "providers": providers,
        },
    }

    emb = cfg.embedding
    emb_key = os.environ.get(emb.api_key_env, "").strip()
    return {
        "agents": agents,
        "embedding": {
            "provider": emb.provider,
            "model": emb.model,
            "dim": emb.dim,
            "key_env": emb.api_key_env,
            "key_present": bool(emb_key),
            "key_masked": registry.mask_key(emb.api_key_env),
        },
        "agent_keys": registry.agent_key_status(active),
        "timeout_s": agent.timeout_s,
        "max_retries": agent.max_retries,
    }


def update_role_settings(
    cfg: Config,
    role: str,
    *,
    provider: str | None,
    model: str | None,
) -> dict:
    """Persist and activate provider/model overrides for one agent role."""
    role = role.strip()
    if role not in ("chunk", "extract", "recall"):
        raise ProviderAdminError("role 必须是 chunk、extract 或 recall")

    updates: dict[str, str] = {}
    provider_value = (provider or "").strip()
    if role == "recall" and provider is not None:
        if provider_value == "" or provider_value in registry.all_provider_ids(cfg):
            updates["MEMORY_AGENT_RECALL_PROVIDER"] = provider_value
    elif role != "recall" and provider_value:
        if provider_value in registry.all_provider_ids(cfg):
            key = {
                "chunk": "MEMORY_AGENT_CHUNK_PROVIDER",
                "extract": "MEMORY_AGENT_EXTRACT_PROVIDER",
            }[role]
            updates[key] = provider_value

    model_value = (model or "").strip()
    if model_value:
        key = {
            "chunk": "MEMORY_AGENT_CHUNK_MODEL",
            "extract": "MEMORY_AGENT_EXTRACT_MODEL",
            "recall": "MEMORY_AGENT_RECALL_MODEL",
        }[role]
        updates[key] = model_value

    if not updates:
        raise ProviderAdminError("缺少 provider 或 model")

    with registry.CUSTOM_LOCK:
        try:
            update_dotenv(cfg.home / ".env", updates)
        except OSError as exc:
            raise ProviderPersistenceError(f"写入 .env 失败: {exc}") from exc

        agent = current_agent(cfg)
        field_by_key = {
            "MEMORY_AGENT_CHUNK_PROVIDER": "chunk_provider",
            "MEMORY_AGENT_EXTRACT_PROVIDER": "extract_provider",
            "MEMORY_AGENT_RECALL_PROVIDER": "recall_provider",
            "MEMORY_AGENT_CHUNK_MODEL": "chunk_model",
            "MEMORY_AGENT_EXTRACT_MODEL": "extract_model",
            "MEMORY_AGENT_RECALL_MODEL": "recall_model",
        }
        for key, value in updates.items():
            agent = replace(agent, **{field_by_key[key]: value})
        _set_current_agent(cfg, agent)

    return {
        "updated": updates,
        "restart_required": True,
        "hint": "provider/model 变更已写入 .env 并同步当前页面;已存在的 LLM 调用路径需重启服务才能全局生效",
    }


def _validate_base_url(base_url: str) -> str:
    if not base_url.startswith("https://") and not base_url.startswith("http://"):
        raise ProviderAdminError("base_url 必须以 http:// 或 https:// 开头")
    return base_url.rstrip("/")


def _base_url_warnings(base_url: str) -> list[str]:
    warnings: list[str] = []
    if not re.search(r"/v\d+", base_url):
        warnings.append(
            f"base_url 不含 /v1 等版本路径,实际请求将指向 "
            f"{base_url}/chat/completions——多数 OpenAI 兼容 API 需要 /v1 前缀,"
            f"请确认这是正确的 API 端点而非 Web 控制台地址。"
            f"例如 DeepSeek 应为 https://api.deepseek.com/v1 而非 https://platform.deepseek.com"
        )
    host = base_url.split("://", 1)[1].split("/")[0]
    if host in ("platform.deepseek.com", "chat.deepseek.com", "platform.openai.com"):
        warnings.append(
            f"{host} 是 Web 平台而非 API 端点;DeepSeek API 为 api.deepseek.com/v1,"
            f"OpenAI API 为 api.openai.com/v1"
        )
    return warnings


def _custom_provider_id(name: str) -> str:
    provider_id = "custom_" + re.sub(
        r"[^a-z0-9_]", "_", name.lower().strip().replace(" ", "_"))
    return re.sub(r"_+", "_", provider_id).strip("_")


def add_custom_provider(cfg: Config, name: str, base_url: str, model: str = "") -> dict:
    """Add an OpenAI-compatible endpoint and activate its runtime catalog entry."""
    name = name.strip()
    base_url = base_url.strip()
    model = model.strip()
    if not name or not base_url:
        raise ProviderAdminError("name 和 base_url 必填")
    base_url = _validate_base_url(base_url)
    warnings = _base_url_warnings(base_url)
    provider_id = _custom_provider_id(name)
    env_var = provider_id.upper() + "_API_KEY"

    with registry.CUSTOM_LOCK:
        existing = registry.load_custom(cfg.home)
        if any(item["id"] == provider_id for item in existing):
            raise ProviderConflictError(f"provider id {provider_id!r} 已存在")

        custom = {
            "id": provider_id,
            "name": name,
            "base_url": base_url,
            "api_key_env": env_var,
            "default_model": model,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        # Preserve the established partial-failure semantics: reserve the key
        # first, then atomically replace the provider catalog.
        update_dotenv(cfg.home / ".env", {env_var: registry.PLACEHOLDER_KEY})
        existing.append(custom)
        registry.save_custom(cfg.home, existing)

        agent = current_agent(cfg)
        custom_map = dict(agent.custom_providers)
        custom_map[provider_id] = {
            "base_url": base_url,
            "api_key_env": env_var,
            "default_model": model,
        }
        _set_current_agent(cfg, replace(agent, custom_providers=custom_map))

    return {
        "provider": custom,
        "hint": f"Key 占位已写入 .env 的 {env_var}={registry.PLACEHOLDER_KEY};请到 ~/.memory_system/.env 替换为真实 key 后再测试连接",
        "warnings": warnings or None,
    }


def update_custom_provider(
    cfg: Config,
    provider_id: str,
    *,
    name: str,
    base_url: str,
    model: str,
) -> dict:
    """Update mutable custom-provider metadata without migrating its key name."""
    provider_id = provider_id.strip()
    if not provider_id:
        raise ProviderAdminError("缺 id")
    if registry.is_builtin(provider_id):
        raise ProviderForbiddenError(f"内置 provider {provider_id!r} 不可修改")
    name = name.strip()
    base_url = base_url.strip()
    model = model.strip()
    if not name or not base_url:
        raise ProviderAdminError("name 和 base_url 必填")
    base_url = _validate_base_url(base_url)

    with registry.CUSTOM_LOCK:
        existing = registry.load_custom(cfg.home)
        index = next(
            (i for i, item in enumerate(existing) if item.get("id") == provider_id), None)
        if index is None:
            raise ProviderNotFoundError(f"provider {provider_id!r} 不存在")

        custom = dict(existing[index])
        custom["name"] = name
        custom["base_url"] = base_url
        custom["default_model"] = model
        existing[index] = custom
        registry.save_custom(cfg.home, existing)

        agent = current_agent(cfg)
        custom_map = dict(agent.custom_providers)
        custom_map[provider_id] = {
            "base_url": base_url,
            "api_key_env": custom["api_key_env"],
            "default_model": model,
        }
        _set_current_agent(cfg, replace(agent, custom_providers=custom_map))

    return {"provider": custom}


def remove_custom_provider(cfg: Config, provider_id: str) -> dict:
    """Remove a custom provider and clear every dangling role override."""
    provider_id = provider_id.strip()
    if not provider_id:
        raise ProviderAdminError("缺 id")
    if registry.is_builtin(provider_id):
        raise ProviderForbiddenError(f"内置 provider {provider_id!r} 不可删除")

    with registry.CUSTOM_LOCK:
        existing = registry.load_custom(cfg.home)
        custom = next((item for item in existing if item["id"] == provider_id), None)
        if custom is None:
            raise ProviderNotFoundError(f"provider {provider_id!r} 不存在")

        # Keep the key variable for possible future reuse.  Catalog replacement
        # remains first, matching the old handler's failure ordering.
        registry.save_custom(
            cfg.home, [item for item in existing if item["id"] != provider_id])

        agent = current_agent(cfg)
        custom_map = dict(agent.custom_providers)
        custom_map.pop(provider_id, None)
        agent = replace(agent, custom_providers=custom_map)
        env_updates: dict[str, str] = {}
        if agent.provider == provider_id:
            env_updates["MEMORY_AGENT_PROVIDER"] = "claude_cli"
            agent = replace(agent, provider="claude_cli")
        if agent.chunk_provider == provider_id:
            env_updates["MEMORY_AGENT_CHUNK_PROVIDER"] = ""
            agent = replace(agent, chunk_provider="")
        if agent.extract_provider == provider_id:
            env_updates["MEMORY_AGENT_EXTRACT_PROVIDER"] = ""
            agent = replace(agent, extract_provider="")
        if agent.recall_provider == provider_id:
            env_updates["MEMORY_AGENT_RECALL_PROVIDER"] = ""
            agent = replace(agent, recall_provider="")
        if env_updates:
            update_dotenv(cfg.home / ".env", env_updates)
        _set_current_agent(cfg, agent)

    return {"removed": provider_id}
