"""配置:数据主目录布局 + embedding provider 设置。

主目录默认 ~/.memory_system,可用 MEMORY_SYSTEM_HOME 覆盖。
embedding 的 base_url/model/dim 走配置,不硬编码(换 workspace 只改这里)。
key 永远从环境变量读,绝不落盘。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
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
class AgentConfig:
    """切块/提取/重构等 LLM agent 的后端设置(与 embedding 分开)。

    provider=claude_cli 走本机 `claude -p`(复用订阅、不烧 key);deepseek/qwen 等
    OpenAI 兼容口走 urllib;fake 离线确定性供测试。按角色定默认模型:切块结构活用
    sonnet 省钱,提取用 opus。key 永远从环境读,绝不落盘、绝不经前端。

    custom_providers: 用户通过控制台添加的自定义 OpenAI 兼容端点。
    字典 key=provider_id, value={base_url, api_key_env, default_model}。
    """

    provider: str = "claude_cli"       # 默认 provider(切块/提取未单独设时回退)
    chunk_provider: str = ""           # 切块专用 provider;空则回退到 provider
    extract_provider: str = ""         # 提取专用 provider;空则回退到 provider
    chunk_model: str = "sonnet"        # S3 切块默认(opus 太烧)
    extract_model: str = "opus"        # S4 提取默认
    recall_model: str = "sonnet"       # S6 重构默认(候选集已定死,只做表达;检索路径求快省)
    # OpenAI 兼容后端(deepseek/qwen 等);claude_cli/fake 不读。
    base_url: str = "https://api.deepseek.com/v1"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_s: int = 90
    max_retries: int = 2          # 首次失败后再试的次数
    custom_providers: dict = field(default_factory=dict)
    # {provider_id: {base_url, api_key_env, default_model}},控制台动态添加

    def provider_for(self, role: str) -> str:
        """返回 role 的有效 provider:专用 > 默认。role ∈ {chunk, extract}。"""
        if role == "chunk" and self.chunk_provider:
            return self.chunk_provider
        if role == "extract" and self.extract_provider:
            return self.extract_provider
        return self.provider


def _agent_from_env() -> AgentConfig:
    d = AgentConfig()
    return AgentConfig(
        provider=os.environ.get("MEMORY_AGENT_PROVIDER", d.provider),
        chunk_provider=os.environ.get("MEMORY_AGENT_CHUNK_PROVIDER", ""),
        extract_provider=os.environ.get("MEMORY_AGENT_EXTRACT_PROVIDER", ""),
        chunk_model=os.environ.get("MEMORY_AGENT_CHUNK_MODEL", d.chunk_model),
        extract_model=os.environ.get("MEMORY_AGENT_EXTRACT_MODEL", d.extract_model),
        recall_model=os.environ.get("MEMORY_AGENT_RECALL_MODEL", d.recall_model),
        base_url=os.environ.get("MEMORY_AGENT_BASE_URL", d.base_url),
        api_key_env=os.environ.get("MEMORY_AGENT_KEY_ENV", d.api_key_env),
        timeout_s=int(os.environ.get("MEMORY_AGENT_TIMEOUT", str(d.timeout_s))),
        max_retries=int(os.environ.get("MEMORY_AGENT_MAX_RETRIES", str(d.max_retries))),
    )


@dataclass(frozen=True)
class RecallConfig:
    """S6 检索层参数(惰性衰减 + 三路检索 + 开场注入)。全部现算,改这里即全库生效、零迁移。

    半衰期起步值 14/90/365 天(tier 1/2/3);其余槽位/预算旋钮见 s6_build_plan §3。
    字段用字面量默认;环境变量(MEMORY_RECALL_*)的读取在 _recall_from_env(load 时),不在 import 时。
    """

    half_life_days: tuple[float, float, float] = (14.0, 90.0, 365.0)  # tier 1/2/3
    topk_final: int = 3            # 情景检索主槽最终条数
    candidate_multiplier: int = 4  # 两路各取 topk_final*4 进 RRF
    rrf_k: int = 60
    w_activation: float = 0.3      # 衰减乘子权重: score * (1 + w * activation)
    same_source_span: int = 1      # 同源扩展前后各取几条
    assoc_limit: int = 2           # 联想槽上限
    detail_limit: int = 5          # 细节检索返回条数
    window_tokens: int = 48        # FTS snippet 窗宽(FTS5 上限 64)
    opening_max_items: int = 3     # 开场硬顶
    opening_token_budget: int = 250
    # S6 Phase 2:session 去重 / 跨 session 冷却(仅作用于 episode 检索;无 session_key 全关)。
    dedup_session: bool = True     # session_key 存在时是否硬去重(总开关)
    cooldown_hours: float = 24.0   # 跨 session 冷却窗口;<=0 关闭
    cooldown_factor: float = 0.8   # 冷却乘子;1.0 关闭


def _env_bool(name: str, default: bool) -> bool:
    # 环境变量解析布尔;缺省或坏值回落默认,不让坏配置炸检索。
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_float(name: str, default: float) -> float:
    # 环境变量解析浮点;缺省或坏值回落默认。
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_half_lives(raw: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
    # "14,90,365" → (14.0, 90.0, 365.0);格式不对回落默认,不让坏配置炸检索。
    try:
        parts = [float(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return default
    return (parts[0], parts[1], parts[2]) if len(parts) >= 3 else default


def _recall_from_env() -> RecallConfig:
    d = RecallConfig()
    return RecallConfig(
        half_life_days=_parse_half_lives(
            os.environ.get("MEMORY_RECALL_HALF_LIVES", ""), d.half_life_days),
        topk_final=int(os.environ.get("MEMORY_RECALL_TOPK_FINAL", str(d.topk_final))),
        candidate_multiplier=int(os.environ.get(
            "MEMORY_RECALL_CANDIDATE_MULTIPLIER", str(d.candidate_multiplier))),
        rrf_k=int(os.environ.get("MEMORY_RECALL_RRF_K", str(d.rrf_k))),
        w_activation=float(os.environ.get("MEMORY_RECALL_W_ACTIVATION", str(d.w_activation))),
        same_source_span=int(os.environ.get(
            "MEMORY_RECALL_SAME_SOURCE_SPAN", str(d.same_source_span))),
        assoc_limit=int(os.environ.get("MEMORY_RECALL_ASSOC_LIMIT", str(d.assoc_limit))),
        detail_limit=int(os.environ.get("MEMORY_RECALL_DETAIL_LIMIT", str(d.detail_limit))),
        window_tokens=int(os.environ.get("MEMORY_RECALL_WINDOW_TOKENS", str(d.window_tokens))),
        opening_max_items=int(os.environ.get(
            "MEMORY_RECALL_OPENING_MAX_ITEMS", str(d.opening_max_items))),
        opening_token_budget=int(os.environ.get(
            "MEMORY_RECALL_OPENING_TOKEN_BUDGET", str(d.opening_token_budget))),
        dedup_session=_env_bool("MEMORY_RECALL_DEDUP_SESSION", d.dedup_session),
        cooldown_hours=_env_float("MEMORY_RECALL_COOLDOWN_HOURS", d.cooldown_hours),
        cooldown_factor=_env_float("MEMORY_RECALL_COOLDOWN_FACTOR", d.cooldown_factor),
    )


@dataclass(frozen=True)
class Config:
    home: Path = field(default_factory=_home)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)
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
    def staging_dir(self) -> Path:
        return self.home / "staging"

    @property
    def chunks_dir(self) -> Path:
        # 切块工作态:每个 session 一个 <session>.json(可丢弃,非正本)。
        return self.staging_dir / "chunks"

    @property
    def staging_episodes_dir(self) -> Path:
        # S4 提取工作态:每个 session 一个 <session>.json(五件套,未审,非正本)。
        # 正本是 active 碎片;S5 确认时才写碎片。删了不影响记忆正本。
        return self.staging_dir / "episodes"

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

    @property
    def imports_dir(self) -> Path:
        # 前端上传导入的 jsonl 落点(transcripts_root 之外的第二发现根)。
        # 不污染 Claude 自己的 ~/.claude/projects;与正本/工作态无关,可删。
        return self.home / "imports"

    def all_dirs(self) -> list[Path]:
        return [
            self.home,
            self.fragments_dir,
            self.episodes_dir,
            self.nodes_dir,
            self.staging_dir,
            self.chunks_dir,
            self.staging_episodes_dir,
            self.opening_cache_dir,
            self.logs_dir,
            self.diagnostics_dir,
            self.preview_cache_dir,
            self.imports_dir,
        ]


def load_config() -> Config:
    # 先确定主目录,再从 主目录/.env 灌环境(已 export 的优先),最后读 embedding 配置。
    # MEMORY_SYSTEM_HOME 决定 .env 的位置,故不从 .env 取(鸡生蛋)。
    # registry 在函数内 import:它的 custom_map 是 provider 知识的单一来源,顶层 import
    # 会与 agent/__init__ 的 `from memory_system.config import AgentConfig` 形成循环。
    from memory_system.agent import registry
    from memory_system.env import load_dotenv

    home = _home()
    load_dotenv(home / ".env")
    agent_cfg = _agent_from_env()
    agent_cfg = replace(agent_cfg, custom_providers=registry.custom_map(home))
    return Config(home=home, embedding=_embedding_from_env(), agent=agent_cfg,
                  recall=_recall_from_env())
