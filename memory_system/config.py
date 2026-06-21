"""配置:数据主目录布局 + embedding provider 设置。

主目录默认 ~/.memory_system,可用 MEMORY_SYSTEM_HOME 覆盖。
embedding 的 base_url/model/dim 走配置,不硬编码(换 workspace 只改这里)。
key 永远从环境变量读,绝不落盘。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    env = os.environ.get("MEMORY_SYSTEM_HOME")
    return Path(env).expanduser() if env else Path.home() / ".memory_system"


def _transcripts_root() -> Path:
    # Claude Code transcript 根目录;每个 cwd 一个 <encoded-cwd> 子目录,内含 *.jsonl。
    env = os.environ.get("MEMORY_TRANSCRIPTS_ROOT")
    return Path(env).expanduser() if env else Path.home() / ".claude" / "projects"


@dataclass(frozen=True)
class EmbeddingConfig:
    # 默认:DashScope text-embedding-v4,1024 维(已实测)。provider=fake 走离线确定性假向量。
    # 字段用字面量默认;环境变量/.env 的读取在 _embedding_from_env(load 时),不在 import 时。
    provider: str = "dashscope"
    model: str = "text-embedding-v4"
    dim: int = 1024
    # 实测用的是 workspace 专属 MaaS 域名;换 workspace 改这里或设环境变量。
    base_url: str = "https://ws-0rc5n2o7rajktheg.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    api_key_env: str = "DASHSCOPE_API_KEY"
    batch_size: int = 10  # DashScope 单请求上限约 10


def _embedding_from_env() -> EmbeddingConfig:
    d = EmbeddingConfig()
    return EmbeddingConfig(
        provider=os.environ.get("MEMORY_EMBED_PROVIDER", d.provider),
        model=os.environ.get("MEMORY_EMBED_MODEL", d.model),
        dim=int(os.environ.get("MEMORY_EMBED_DIM", str(d.dim))),
        base_url=os.environ.get("MEMORY_EMBED_BASE_URL", d.base_url),
        api_key_env=os.environ.get("MEMORY_EMBED_KEY_ENV", d.api_key_env),
        batch_size=int(os.environ.get("MEMORY_EMBED_BATCH", str(d.batch_size))),
    )


@dataclass(frozen=True)
class Config:
    home: Path = field(default_factory=_home)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    transcripts_root: Path = field(default_factory=_transcripts_root)

    # ---- 主目录布局 ----
    @property
    def db_path(self) -> Path:
        return self.home / "memory.db"

    @property
    def fragments_dir(self) -> Path:
        return self.home / "fragments"

    @property
    def episodes_dir(self) -> Path:
        return self.fragments_dir / "episodes"

    @property
    def nodes_dir(self) -> Path:
        return self.fragments_dir / "nodes"

    @property
    def opening_cache_dir(self) -> Path:
        return self.home / "opening_cache"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def diagnostics_dir(self) -> Path:
        return self.home / "diagnostics"

    @property
    def preview_cache_dir(self) -> Path:
        # jsonl 预览的可丢弃派生缓存(S2 用),按 路径+mtime 失效。
        return self.home / "cache" / "jsonl_preview"

    def all_dirs(self) -> list[Path]:
        return [
            self.home,
            self.fragments_dir,
            self.episodes_dir,
            self.nodes_dir,
            self.opening_cache_dir,
            self.logs_dir,
            self.diagnostics_dir,
            self.preview_cache_dir,
        ]


def load_config() -> Config:
    # 先确定主目录,再从 主目录/.env 灌环境(已 export 的优先),最后读 embedding 配置。
    # MEMORY_SYSTEM_HOME 决定 .env 的位置,故不从 .env 取(鸡生蛋)。
    from memory_system.env import load_dotenv

    home = _home()
    load_dotenv(home / ".env")
    return Config(home=home, embedding=_embedding_from_env())
