"""重构 agent 封装(S6-5):结构化检索结果 → 一段自然语言回忆。

确定性边界(s6_build_plan §0.2)的执行点:候选集在进本模块之前已由检索管线定死;
本模块在调用 provider 前把候选集(public_id + 槽位)写进日志——召回可重放。
这里只做**表达**:候选集内怎么取舍、揉合归重构 LLM,槽位权限与防虚构约束写在 prompt 里
(prompt 是核心调参对象,独立文件不硬编码,照既有惯例)。

重构 LLM 的输入固定三部分(§0.2 铁律):
  重构 system prompt + 结构化检索结果(JSON 美化)+ 用户当轮 query。

细节检索不接重构:开窗就是它的默认输出,语义重构会毁掉逐字保真(§4 S6-5)。
失败抛 ChatError,由调用方(CLI)决定降级——回落 --raw 结构化输出,不吞结果。
"""

from __future__ import annotations

import json
from pathlib import Path

from memory_system.agent import get_chat_provider
from memory_system.config import Config
from memory_system.log import get_logger

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILES = {
    "episode": "recall_episode_system.txt",
    "concept": "recall_concept_system.txt",
    "opening": "opening_system.txt",
}


def _candidates(mode: str, structured: dict) -> dict:
    """候选集摘要(public_id 列表 + 槽位),写日志用;重放时据此还原这次召回。"""
    if mode in ("episode", "opening"):
        # episode 有 frame_nodes、opening 有 token_budget;两者都是"槽位→public_id 列表"。
        extra = ({"frame_nodes": structured.get("frame_nodes", [])} if mode == "episode"
                 else {"token_budget": structured.get("token_budget")})
        return {**extra,
                **{slot: [e.get("public_id") for e in items]
                   for slot, items in structured.get("slots", {}).items()}}
    return {"node": structured.get("node"),
            "alias_bridge": structured.get("alias_bridge"),
            "episodes": [e.get("public_id") for e in structured.get("episodes", [])]}


def run(cfg: Config, mode: str, structured: dict, user_query: str, *, provider=None) -> str:
    """跑一次重构。mode ∈ {episode, concept}。后端失败抛 ChatError(调用方决定降级)。

    provider 形参供测试注入(fake 计数/注错);默认走 get_chat_provider(cfg.agent)。
    """
    if mode not in _PROMPT_FILES:
        raise ValueError(f"未知重构 mode: {mode!r}(细节检索不接重构)")
    system = (_PROMPT_DIR / _PROMPT_FILES[mode]).read_text(encoding="utf-8")
    user = ("## 结构化检索结果\n"
            + json.dumps(structured, ensure_ascii=False, indent=2)
            + "\n\n## 用户当轮 query\n" + user_query)
    # 候选集日志:重构调用之前落盘(即使重构失败,这次召回也可重放)。
    get_logger().info("recall %s 重构候选集(可重放): %s", mode,
                      json.dumps(_candidates(mode, structured), ensure_ascii=False))
    if provider is None:
        provider = get_chat_provider(cfg.agent)
    res = provider.complete(system, user, model=cfg.agent.recall_model,
                            timeout=cfg.agent.timeout_s)
    return res.text.strip()
