"""过程 prompt 正本读写(控制台「过程 Prompt」区块的后端)。

系统四类过程的 prompt 都是独立文件正本(memory_system/prompts/*.txt),不进 DB、
可版本化(在 git 仓库内,改动即历史,不做备份机制)。控制台允许在线编辑:

铁律(硬约束):
- **白名单**:只认下面八个键,各自映射到固定文件名;任何路径成分/越权名一律拒(_resolve 抛
  PromptError → server 回 400)。键本身不含路径分隔符,文件名写死,杜绝目录穿越。
- **content 非空**:strip 后为空即拒(空 prompt 会废掉整个过程)。
- **原子写**:tmp + os.replace(照 fragments._atomic_write_text 惯例),写盘中途崩溃绝不留半截。

生效时机:切块/提取的 prompt 加载走 @lru_cache(chunk.load_chunk_prompt /
extract.load_extract_prompt),写回后必须清缓存才即时生效;重构(recall.reconstruct.run)每次
现读、无缓存;MCP 选路描述也由 tools/list 每次现读。write_prompt 统一在写盘后清对应
缓存,故写回后服务端立即可读;MCP 客户端通常缓存 tools/list,编辑后下一次会话生效。
"""

from __future__ import annotations

import os
from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# 键 → (固定文件名, 所属过程)。键即白名单;文件名写死,不接受任何外来路径成分。
_PROMPTS: dict[str, tuple[str, str]] = {
    "chunk_system": ("chunk_system.txt", "chunk"),
    "extract_system": ("extract_system.txt", "extract"),
    "recall_episode_system": ("recall_episode_system.txt", "recall"),
    "recall_concept_system": ("recall_concept_system.txt", "recall"),
    "opening_system": ("opening_system.txt", "recall"),
    "tool_episode_desc": ("tool_episode_desc.txt", "mcp"),
    "tool_detail_desc": ("tool_detail_desc.txt", "mcp"),
    "tool_concept_desc": ("tool_concept_desc.txt", "mcp"),
}

# 过程展示名(前端分组标签用)。
PROCESS_LABELS = {"chunk": "切块", "extract": "提取", "recall": "重构", "mcp": "选路"}


class PromptError(ValueError):
    """prompt 名越权(白名单外)或 content 校验失败。server 折成 400。"""


def _resolve(name: str) -> Path:
    """把 prompt 键解析成固定文件路径;白名单外抛 PromptError(堵越权/路径穿越)。"""
    entry = _PROMPTS.get(name)
    if entry is None:
        raise PromptError(f"未知 prompt: {name!r}(仅允许 {sorted(_PROMPTS)})")
    return _PROMPT_DIR / entry[0]


def list_prompts() -> list[dict]:
    """列出八个 prompt:{name, process, process_label, filename, content}。

    读不到(理论上不会,正本随包发布)按空串给出,不 500。
    """
    out: list[dict] = []
    for name, (fname, process) in _PROMPTS.items():
        p = _PROMPT_DIR / fname
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            content = ""
        out.append({
            "name": name,
            "process": process,
            "process_label": PROCESS_LABELS.get(process, process),
            "filename": fname,
            "content": content,
        })
    return out


def read_prompt(name: str) -> str:
    """读单个 prompt 正本(白名单外抛 PromptError)。"""
    return _resolve(name).read_text(encoding="utf-8")


def write_prompt(name: str, content: str) -> None:
    """写回一个 prompt 正本。白名单校验 + content 非空 + 原子写 + 清对应 lru 缓存。

    name 越权或 content 空 → PromptError(server 回 400)。写盘 OSError 由 server 折 500。
    """
    p = _resolve(name)  # 白名单校验(越权名在此抛 PromptError)
    if not isinstance(content, str) or not content.strip():
        raise PromptError("prompt content 不能为空(空 prompt 会废掉整个过程)")
    _atomic_write_text(p, content)
    _invalidate_cache(name)


def _atomic_write_text(p: Path, text: str) -> None:
    """tmp + os.replace 原子替换(照 fragments._atomic_write_text 惯例);.txt.tmp 不入白名单。"""
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _invalidate_cache(name: str) -> None:
    """清切块/提取 prompt 的 @lru_cache;重构与 MCP 选路每次现读,无需清。

    函数内 import:prompt_store 顶层不依赖 chunk/extract(避免加载期耦合),只在真写这两个
    prompt 时才触碰它们的缓存清理接口。
    """
    if name == "chunk_system":
        from memory_system.chunk import load_chunk_prompt
        load_chunk_prompt.cache_clear()
    elif name == "extract_system":
        from memory_system.extract import load_extract_prompt
        load_extract_prompt.cache_clear()
