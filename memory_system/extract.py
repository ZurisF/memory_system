"""提取(Prompt 2)—— 把确认后的每个段独立提取成五件套。

链路:render_source_text(ct, 段) → <source_text> + <existing_nodes> → 提取 agent →
extract_json → 五件套契约**严校** → ExtractResult。

五件套:overview / summary / highlights(0-3 逐字)/ nodes(三选一)/ salience_tier(1-3)。

- existing_nodes 读 active node 碎片,喂 agent 做 命中/别名/新建 三选一。
- **按块回滚**(extract_segments):逐段提取,坏段不拖好段——N 成 M 坏 → N 进 staging、
  M 进 retry,绝不整单回滚、绝不卡审核。
- agent 失败 → 重试 max_retries 次;屡败 → ExtractFailed(带各次错误,供 retry 列表 + 告警)。
- **严校失败即坏响应**:空 overview/summary、highlights>3、salience 越界、node action 非法、
  label/别名含换行(碎片不可写)——一律抛 ValueError 触发重试,绝不静默夹紧。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from memory_system.agent import ChatError, ChatProvider, extract_json
from memory_system.preprocess import CleanedTranscript, render_source_text

_PROMPT_PATH = Path(__file__).parent / "prompts" / "extract_system.txt"

MAX_HIGHLIGHTS = 3
_NODE_ACTIONS = {"match_existing", "add_alias", "new"}
_SALIENCE_TIERS = {1, 2, 3}


class ExtractFailed(RuntimeError):
    """agent 提取多次失败。errors 为各次失败说明,供 retry 列表与告警。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"提取失败 {len(errors)} 次:" + " | ".join(errors))


@dataclass
class ExtractResult:
    overview: str
    summary: str
    highlights: list[dict]          # [{"text":逐字, "tag":一词标签}]
    nodes: list[dict]               # [{"label","action","reason"[,"new_alias"]}]
    salience_tier: int
    salience_reason: str
    provider: str
    model: str
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    duration_ms: int | None = None
    attempts: int = 1


@dataclass
class ExtractBatch:
    """按块回滚的产物:好块进 staged,坏块进 failed,互不拖累。"""

    staged: list[tuple[dict, ExtractResult, str]] = field(default_factory=list)  # (段, 结果, source_text)
    failed: list[tuple[dict, list[str]]] = field(default_factory=list)           # (段, errors)


@lru_cache(maxsize=1)
def load_extract_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


# ---- existing_nodes:读 active node 碎片喂三选一 ----

def existing_nodes(nodes_dir: Path) -> list[dict]:
    """读 active node 碎片 → [{label, aliases}]。S4 阶段多半为空 → 所有 node 判 new。"""
    from memory_system.fragments import load_all_nodes

    return [{"label": nd.label, "aliases": list(nd.aliases)}
            for _, nd in load_all_nodes(nodes_dir)]


def render_existing_nodes(nodes: list[dict]) -> str:
    if not nodes:
        return "(暂无已有 node)"
    lines = []
    for nd in nodes:
        al = nd.get("aliases") or []
        lines.append(f"- {nd['label']}(别名: {', '.join(al)})" if al else f"- {nd['label']}")
    return "\n".join(lines)


# ---- 五件套严校 ----

def _no_newline(key: str, value: str) -> str:
    """label/别名将进碎片 frontmatter(逐行格式),含换行写不回来 → 早报错触发重试。"""
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} 含换行,碎片不可写: {value!r}")
    return value


def _validate_highlights(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"highlights 非列表: {raw!r}")
    if len(raw) > MAX_HIGHLIGHTS:
        raise ValueError(f"highlights {len(raw)} 条 > 上限 {MAX_HIGHLIGHTS}(宁缺毋滥)")
    out = []
    for h in raw:
        if not isinstance(h, dict):
            raise ValueError(f"highlight 非对象: {h!r}")
        text = h.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"highlight 缺 text: {h!r}")
        # text 逐字保留(不 strip),tag 取一词标签
        out.append({"text": text, "tag": str(h.get("tag", "")).strip()})
    return out


def _validate_nodes(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"nodes 非列表: {raw!r}")
    out = []
    for nd in raw:
        if not isinstance(nd, dict):
            raise ValueError(f"node 非对象: {nd!r}")
        label = str(nd.get("label", "")).strip()
        action = str(nd.get("action", "")).strip()
        if not label:
            raise ValueError(f"node 缺 label: {nd!r}")
        _no_newline("node label", label)
        if action not in _NODE_ACTIONS:
            raise ValueError(f"node action 非法: {action!r}(应为 {sorted(_NODE_ACTIONS)})")
        item = {"label": label, "action": action, "reason": str(nd.get("reason", "")).strip()}
        if action == "add_alias":
            alias = str(nd.get("new_alias", "")).strip()
            if not alias:
                raise ValueError(f"add_alias 缺 new_alias: {nd!r}")
            item["new_alias"] = _no_newline("node new_alias", alias)
        out.append(item)
    return out


def _parse_extraction(obj: dict) -> dict:
    overview = str(obj.get("overview", "")).strip()
    if not overview:
        raise ValueError("overview 为空")
    summary = str(obj.get("summary", "")).strip()
    if not summary:
        raise ValueError("summary 为空")
    highlights = _validate_highlights(obj.get("highlights"))
    nodes = _validate_nodes(obj.get("nodes"))
    tier_raw = obj.get("salience_tier")
    try:
        tier = int(tier_raw)
    except (TypeError, ValueError):
        raise ValueError(f"salience_tier 非整数: {tier_raw!r}") from None
    if tier not in _SALIENCE_TIERS:
        raise ValueError(f"salience_tier 越界: {tier}(应 1-3)")
    return {
        "overview": overview,
        "summary": summary,
        "highlights": highlights,
        "nodes": nodes,
        "salience_tier": tier,
        "salience_reason": str(obj.get("salience_reason", "")).strip(),
    }


def run_extract(
    source_text: str,
    nodes: list[dict],
    provider: ChatProvider,
    *,
    model: str,
    timeout: int,
    max_retries: int,
) -> ExtractResult:
    """对一段 source_text 调提取 agent,严校五件套。屡败抛 ExtractFailed。"""
    system = load_extract_prompt()
    user = (
        f"<source_text>\n{source_text}\n</source_text>\n\n"
        f"<existing_nodes>\n{render_existing_nodes(nodes)}\n</existing_nodes>"
    )
    errors: list[str] = []
    for attempt in range(1, max_retries + 2):
        try:
            res = provider.complete(system, user, model=model, timeout=timeout)
            obj = extract_json(res.text)
            fields = _parse_extraction(obj)
            return ExtractResult(
                **fields, provider=provider.id, model=res.model,
                usage=res.usage, cost_usd=res.cost_usd,
                duration_ms=res.duration_ms, attempts=attempt,
            )
        except (ChatError, ValueError) as e:
            errors.append(f"第{attempt}次: {e}")
    raise ExtractFailed(errors)


def extract_segments(
    ct: CleanedTranscript,
    segments: list[dict],
    provider: ChatProvider,
    nodes: list[dict],
    *,
    model: str,
    timeout: int,
    max_retries: int,
    max_workers: int = 1,
    on_staged: Callable[[dict, ExtractResult, str], None] | None = None,
    on_failed: Callable[[dict, list[str]], None] | None = None,
) -> ExtractBatch:
    """按块回滚:逐段独立提取,坏段进 failed 不中断;好段进 staged。

    每段从其回合区间渲染 source_text(同源、逐字),提取结果与原文一并交给上层落 staging。

    **并发 + 逐条落盘**(max_workers>1 时):每段的慢 I/O(LLM 调用)在线程池里并发跑,
    但结果消费(append batch + on_staged/on_failed 回调)一律回到**主线程串行**执行
    (`as_completed` 在调用线程逐个 yield)——故回调即落盘也无需锁、不竞争 staging 文件。
    每段一完成立即经回调落盘 ⇒ 批量提取中途退出,已完成的段不丢。
    provider 实现都是无状态的(claude_cli 每调起独立子进程、openai_compat urllib 单发),
    并发安全。max_workers=1(默认)= 纯顺序,确定性,供 CLI / 行为脚本测试沿用。
    """
    batch = ExtractBatch()
    if not segments:
        return batch

    def work(seg: dict) -> tuple[str, dict, object, str | None]:
        """线程内只做慢活:渲染 + 提取;不碰共享状态、不落盘。"""
        src = render_source_text(ct, seg["start_turn"], seg["end_turn"])
        try:
            res = run_extract(src, nodes, provider, model=model,
                              timeout=timeout, max_retries=max_retries)
            return ("staged", seg, res, src)
        except ExtractFailed as e:
            return ("failed", seg, e.errors, None)

    def consume(outcome: tuple[str, dict, object, str | None]) -> None:
        """主线程串行消费:记 batch + 即时回调落盘。"""
        kind, seg, payload, src = outcome
        if kind == "staged":
            batch.staged.append((seg, payload, src))
            if on_staged is not None:
                on_staged(seg, payload, src)
        else:
            batch.failed.append((seg, payload))
            if on_failed is not None:
                on_failed(seg, payload)

    workers = max(1, min(max_workers, len(segments)))
    if workers == 1:
        for seg in segments:
            consume(work(seg))
    else:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="extract") as ex:
            futures = [ex.submit(work, seg) for seg in segments]
            for fut in as_completed(futures):
                consume(fut.result())
    return batch
