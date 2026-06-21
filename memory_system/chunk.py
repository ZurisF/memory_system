"""切块(Prompt 1)—— 把清洗后的对话切成叙事弧线闭合的段。

链路:render_for_chunk(ct) → 回合编号文本 → 切块 agent → extract_json → 校验 →
每段回合区间 [start_turn, end_turn] 直接回映 covered_uuids。

回合是切块与回映 uuid 的统一单位:agent 输出回合号,不数行(杜绝多行消息的计数错位)。

- 手动切块(manual_segments)不走 agent,始终可用。
- 超大输入 → OversizedError(带粗分建议),绝不静默截断。
- agent 失败 → 重试 max_retries 次;屡败 → ChunkFailed(带各次错误,供 retry 列表 + UI 告警)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from memory_system.agent import ChatError, ChatProvider, extract_json
from memory_system.preprocess import CleanedTranscript, render_for_chunk

MAX_CHARS = 120_000  # 渲染文本超此即报错让人工先粗分(opus 200k token 窗够,但再大切块质量掉)
SHORT_TURNS = 15     # 少于此回合数算 short(与 prompt 一致)

_TURN_REF = re.compile(r"\s*(?:回合|turn|t|#)?\s*(\d+)\s*", re.IGNORECASE)
_PROMPT_PATH = Path(__file__).parent / "prompts" / "chunk_system.txt"


class OversizedError(RuntimeError):
    """输入过大,需人工先粗分。"""

    def __init__(self, chars: int, limit: int) -> None:
        self.chars = chars
        self.limit = limit
        super().__init__(
            f"对话渲染后 {chars} 字符 > 上限 {limit};请先人工粗分(按回合分成几块)"
            f"再逐块切。绝不静默截断。"
        )


class ChunkFailed(RuntimeError):
    """agent 切块多次失败。errors 为各次失败说明,供 retry 列表与告警。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"切块失败 {len(errors)} 次:" + " | ".join(errors))


@dataclass
class ChunkResult:
    segments: list[dict]            # 规范化段(无 seg_id,留给 store 分配)
    provider: str
    model: str
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    duration_ms: int | None = None
    attempts: int = 1


@lru_cache(maxsize=1)
def load_chunk_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def uuids_by_turn(ct: CleanedTranscript) -> dict[int, list[str]]:
    return {t.idx: [u for u in t.uuids if u] for t in ct.turns}


def _parse_turn_ref(v) -> int:
    """把 5 / '5' / '回合5' / 'T5' 解析成回合号整数;非整数或无法解析抛 ValueError。

    宽容 agent 的格式包裹(回合/turn/T/# 前缀),但不接受小数、夹带其它字符:'5.5'、
    'abc'、'1-2' 都报错(P1-A:坏回合号走重试,不蒙混)。
    """
    if isinstance(v, bool):  # bool 是 int 子类,单独挡掉
        raise ValueError(f"回合号非整数: {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v.is_integer():
            return int(v)
        raise ValueError(f"回合号非整数: {v!r}")
    m = _TURN_REF.fullmatch(str(v))
    if not m:
        raise ValueError(f"无法解析回合号: {v!r}")
    return int(m.group(1))


def _covered_uuids(start: int, end: int, umap: dict[int, list[str]]) -> list[str]:
    out: list[str] = []
    for i in range(start, end + 1):
        out.extend(umap.get(i, []))
    return out


def _normalize_segment(raw: dict, n_turns: int, umap: dict[int, list[str]], *, origin: str) -> dict:
    if "start" not in raw or "end" not in raw:
        raise ValueError(f"段缺 start/end: {raw!r}")
    a = _parse_turn_ref(raw["start"])
    b = _parse_turn_ref(raw["end"])
    # agent 路径严校(P1-A):回合号必须落在真实回合集合内、start<=end。坏边界抛 ValueError
    # 走现有重试/告警,绝不静默交换或夹紧——否则 0-999 会被当成全篇、50-20 被悄悄翻面。
    # (manual_segments 的友好夹紧/交换是另一条路径,与此分开,见该函数。)
    if a not in umap or b not in umap:
        raise ValueError(f"段边界越界 {a}-{b}(真实回合 1-{n_turns})")
    if a > b:
        raise ValueError(f"段边界逆序 start={a} > end={b}")
    deletions = raw.get("deletions") or []
    if not isinstance(deletions, list):
        raise ValueError(f"deletions 非列表: {deletions!r}")
    return {
        "start_turn": a,
        "end_turn": b,
        "tag": str(raw.get("tag", "")).strip(),
        "cut_reason": str(raw.get("cut_reason", "")).strip(),
        "short": bool(raw.get("short", (b - a + 1) < SHORT_TURNS)),
        "deletions": [
            {"range": str(d.get("range", "")), "reason": str(d.get("reason", ""))}
            for d in deletions if isinstance(d, dict)
        ],
        "origin": origin,
        "covered_uuids": _covered_uuids(a, b, umap),
    }


def _parse_segments(obj: dict, n_turns: int, umap: dict[int, list[str]]) -> list[dict]:
    segs = obj.get("segments")
    if not isinstance(segs, list) or not segs:
        raise ValueError(f"响应无 segments 列表: {str(obj)[:200]}")
    out = [_normalize_segment(s, n_turns, umap, origin="agent") for s in segs]
    return sorted(out, key=lambda s: s["start_turn"])


def run_chunk(
    ct: CleanedTranscript,
    provider: ChatProvider,
    *,
    model: str,
    timeout: int,
    max_retries: int,
    max_chars: int = MAX_CHARS,
) -> ChunkResult:
    """调 agent 切块。超大抛 OversizedError;屡败抛 ChunkFailed。"""
    text = render_for_chunk(ct)
    if len(text) > max_chars:
        raise OversizedError(len(text), max_chars)
    n = len(ct.turns)
    umap = uuids_by_turn(ct)
    system = load_chunk_prompt()
    user = f"<conversation>\n{text}\n</conversation>"

    errors: list[str] = []
    for attempt in range(1, max_retries + 2):
        try:
            res = provider.complete(system, user, model=model, timeout=timeout)
            obj = extract_json(res.text)
            segments = _parse_segments(obj, n, umap)
            return ChunkResult(
                segments=segments, provider=provider.id, model=res.model,
                usage=res.usage, cost_usd=res.cost_usd,
                duration_ms=res.duration_ms, attempts=attempt,
            )
        except (ChatError, ValueError) as e:
            errors.append(f"第{attempt}次: {e}")
    raise ChunkFailed(errors)


def _compress_ranges(turns: list[int]) -> list[list[int]]:
    """把零散回合号压成连续区间 [[a,b],...](已排序输入)。"""
    out: list[list[int]] = []
    for n in turns:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def validate_segments(segments: list[dict], turn_idxs: set[int]) -> dict:
    """段间关系校验(P1-B):**禁重叠**(硬错),**允许空洞**(警告)。

    选段本是挑值得记的,不强制首尾全覆盖;但两段回合区间相交会让同一回合重复入库,
    必须拒。越界(回合不在真实集合内)由调用方先挡,这里只管段与段之间。

    返回 {"ok": 无重叠, "overlaps": [(seg_a, seg_b, [起,止]),...], "gaps": [[起,止],...]}。
    gaps 仅作提示(哪些回合没被任何段覆盖),不影响 ok。
    """
    ordered = sorted(segments, key=lambda s: (s["start_turn"], s["end_turn"]))
    overlaps: list[dict] = []
    for i in range(len(ordered)):
        a = ordered[i]
        for b in ordered[i + 1 :]:
            if b["start_turn"] > a["end_turn"]:
                break  # 已排序,后续段更靠后,不会再与 a 相交
            lo = max(a["start_turn"], b["start_turn"])
            hi = min(a["end_turn"], b["end_turn"])
            overlaps.append({
                "a": a.get("seg_id") or f"{a['start_turn']}-{a['end_turn']}",
                "b": b.get("seg_id") or f"{b['start_turn']}-{b['end_turn']}",
                "range": [lo, hi],
            })
    covered: set[int] = set()
    for s in segments:
        covered.update(range(s["start_turn"], s["end_turn"] + 1))
    gaps = _compress_ranges(sorted(turn_idxs - covered))
    return {"ok": not overlaps, "overlaps": overlaps, "gaps": gaps}


def manual_segments(
    ct: CleanedTranscript, boundaries: list[tuple[int, int]]
) -> list[dict]:
    """人工指定回合边界 [(start_turn, end_turn), ...] 直接建段(不走 agent)。"""
    n = len(ct.turns)
    umap = uuids_by_turn(ct)
    out: list[dict] = []
    for start, end in boundaries:
        if start > end:
            start, end = end, start
        start = max(1, min(start, n))
        end = max(1, min(end, n))
        out.append({
            "start_turn": start, "end_turn": end, "tag": "", "cut_reason": "手动切块",
            "short": (end - start + 1) < SHORT_TURNS, "deletions": [],
            "origin": "manual", "covered_uuids": _covered_uuids(start, end, umap),
        })
    return out
