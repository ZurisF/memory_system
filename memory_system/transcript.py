"""transcript 发现层 —— 列出 Claude Code 的 jsonl 对话记录。

根目录 `~/.claude/projects/<encoded-cwd>/*.jsonl`(S0 实测)。
列表只做廉价 stat + 首行嗅探,不全量解析(全量解析走预览缓存)。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

# mtime 距今多少秒内,视为「可能正在写入」,给前端警示(idea_v2 S2)。
WRITING_WINDOW_SEC = 120


@dataclass
class TranscriptInfo:
    path: Path
    session_id: str          # 文件名 stem
    cwd: str | None          # 首条记录里的 cwd(比 encoded 目录名可靠)
    mtime: float
    size: int
    line_count: int          # 廉价代理:行数 ≈ 记录数(非对话回合数)
    maybe_writing: bool      # mtime 在 WRITING_WINDOW 内


def _sniff_cwd(path: Path) -> str | None:
    """读首个含 cwd 的记录,拿 cwd;失败返回 None。只读前若干行,不全量解析。"""
    try:
        with path.open("r", encoding="utf-8") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("cwd"):
                    return rec["cwd"]
    except OSError:
        return None
    return None


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0


def describe(path: Path, *, now: float | None = None) -> TranscriptInfo:
    now = time.time() if now is None else now
    st = path.stat()
    return TranscriptInfo(
        path=path,
        session_id=path.stem,
        cwd=_sniff_cwd(path),
        mtime=st.st_mtime,
        size=st.st_size,
        line_count=_count_lines(path),
        maybe_writing=(now - st.st_mtime) <= WRITING_WINDOW_SEC,
    )


def discover(root: Path, *, now: float | None = None) -> list[TranscriptInfo]:
    """列出 root 下所有 jsonl,按 mtime 倒序(最近的在前)。"""
    if not root.exists():
        return []
    paths = sorted(root.glob("*/*.jsonl"))
    infos = [describe(p, now=now) for p in paths]
    infos.sort(key=lambda i: i.mtime, reverse=True)
    return infos
