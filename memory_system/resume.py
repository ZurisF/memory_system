"""resume 断点识别 —— 靠跨文件 uuid 重叠,不靠 isInitial(实测不在场)。

Claude Code 的 resume/continue 把旧会话复刻进新 jsonl,**message-uuid 原样保留**
(S0 实测)。于是:目标 transcript 里、uuid 已出现在更早 transcript 的那段**连续前缀**,
就是复刻来的;断点 = 其后第一个本会话原生的回合。

best-effort:判错由人审兜底(idea_v2 S2 通过门明确不依赖断点完美)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from memory_system.preprocess import CleanedTranscript


def collect_message_uuids(path: Path) -> set[str]:
    """廉价提取一个 jsonl 的全部 message uuid(只读 uuid 字段,不解析 content)。"""
    out: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("type") in ("user", "assistant"):
                    u = rec.get("uuid")
                    if u:
                        out.add(u)
    except OSError:
        pass
    return out


@dataclass
class ResumeInfo:
    is_resume: bool
    breakpoint_idx: int       # 新内容起始回合 idx(1 起);非 resume 则 1
    copied_turns: int         # 复刻前缀的回合数


def detect_resume(ct: CleanedTranscript, prior_uuids: set[str]) -> ResumeInfo:
    """prior_uuids = 所有更早 transcript 的 message-uuid 并集。

    取 ct 开头「所有 uuid 都已在 prior_uuids」的最长连续回合前缀作复刻段。
    """
    if not prior_uuids:
        return ResumeInfo(is_resume=False, breakpoint_idx=1, copied_turns=0)
    copied = 0
    for t in ct.turns:
        ids = [u for u in t.uuids if u]
        if ids and all(u in prior_uuids for u in ids):
            copied += 1
        else:
            break
    if copied == 0:
        return ResumeInfo(is_resume=False, breakpoint_idx=1, copied_turns=0)
    bp = copied + 1 if copied < len(ct.turns) else copied
    return ResumeInfo(is_resume=True, breakpoint_idx=bp, copied_turns=copied)


def build_prior_uuids(target: Path, older_paths: list[Path]) -> set[str]:
    """汇总所有「比 target 更早」的 transcript 的 uuid(调用方按 mtime 过滤后传入)。"""
    acc: set[str] = set()
    for p in older_paths:
        if p == target:
            continue
        acc |= collect_message_uuids(p)
    return acc
