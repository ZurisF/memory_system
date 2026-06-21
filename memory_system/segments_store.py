"""切块工作态持久化 —— home/staging/chunks/<session>.json。

段是 S3→S4 之间的工作态,**不是正本**(正本是 active 碎片)。删了不影响记忆;
所以存可丢弃的 JSON,不进 DB(守"碎片是正本、SQLite 可重建")。

文档形态:
  { session_id, source_path, source_mtime, created_at, updated_at, seq,
    agent: {provider, model, last_run_at, usage, cost_usd, attempts},
    segments: [ {seg_id, start_turn, end_turn, tag, cut_reason, short,
                 deletions, origin, covered_uuids} ],
    retry: [ {at, provider, model, error} ] }

uuid 只在本工作文件内部(供 S4 提取/去重),不上 UI、不进碎片。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path_for(chunks_dir: Path, session_id: str) -> Path:
    # session_id 是 jsonl 文件 stem(uuid 形态),天然安全文件名;仍做基本兜底。
    safe = session_id.replace("/", "_").replace("..", "_")
    return chunks_dir / f"{safe}.json"


def load(chunks_dir: Path, session_id: str) -> dict | None:
    p = path_for(chunks_dir, session_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write(chunks_dir: Path, doc: dict) -> dict:
    chunks_dir.mkdir(parents=True, exist_ok=True)
    doc["updated_at"] = _now()
    p = path_for(chunks_dir, doc["session_id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)  # 原子落盘
    return doc


def _blank_doc(session_id: str, source_path: str, source_mtime: float | None) -> dict:
    return {
        "session_id": session_id,
        "source_path": source_path,
        "source_mtime": source_mtime,
        "created_at": _now(),
        "updated_at": _now(),
        "seq": 0,
        "agent": None,
        "segments": [],
        "retry": [],
    }


def _assign_ids(doc: dict, segments: list[dict]) -> list[dict]:
    """给缺 seg_id 的段分配稳定 id(doc.seq 单调递增);保留已有 id。"""
    seq = int(doc.get("seq", 0))
    out = []
    for s in segments:
        s = dict(s)
        if not s.get("seg_id"):
            seq += 1
            s["seg_id"] = f"s{seq}"
        out.append(s)
    doc["seq"] = seq
    return out


def save_full(
    chunks_dir: Path,
    session_id: str,
    source_path: str,
    source_mtime: float | None,
    segments: list[dict],
    *,
    agent_meta: dict | None = None,
) -> dict:
    """整份保存段(前端编辑后回存)。保留既有 retry/created_at,刷新 source_mtime。"""
    doc = load(chunks_dir, session_id) or _blank_doc(session_id, source_path, source_mtime)
    doc["source_path"] = source_path
    doc["source_mtime"] = source_mtime
    doc["segments"] = _assign_ids(doc, segments)
    if agent_meta is not None:
        doc["agent"] = agent_meta
    return _write(chunks_dir, doc)


def record_agent_run(
    chunks_dir: Path,
    session_id: str,
    source_path: str,
    source_mtime: float | None,
    result,  # chunk.ChunkResult
) -> dict:
    """切块 agent 成功一次:用其 segments 覆盖,并记 agent 元信息。"""
    agent_meta = {
        "provider": result.provider,
        "model": result.model,
        "last_run_at": _now(),
        "usage": result.usage,
        "cost_usd": result.cost_usd,
        "attempts": result.attempts,
    }
    return save_full(
        chunks_dir, session_id, source_path, source_mtime,
        result.segments, agent_meta=agent_meta,
    )


def append_retry(
    chunks_dir: Path,
    session_id: str,
    source_path: str,
    source_mtime: float | None,
    *,
    provider: str,
    model: str,
    error: str,
) -> dict:
    """记一次切块失败(供 UI 告警 + 人工重试)。不动已有 segments。"""
    doc = load(chunks_dir, session_id) or _blank_doc(session_id, source_path, source_mtime)
    doc.setdefault("retry", []).append(
        {"at": _now(), "provider": provider, "model": model, "error": error}
    )
    return _write(chunks_dir, doc)


# ---- 纯函数式段编辑(CLI/测试用;前端可自行编辑后 save_full)----
# umap: {回合号: [uuid,...]},uuid 不经前端,回存/编辑时一律按回合区间重算。

def recompute_uuids(start: int, end: int, umap: dict[int, list[str]]) -> list[str]:
    """从回合区间重算覆盖的 uuid。"""
    out: list[str] = []
    for i in range(start, end + 1):
        out.extend(umap.get(i, []))
    return out


def merge(segments: list[dict], seg_ids: list[str], umap: dict[int, list[str]]) -> list[dict]:
    """合并指定相邻段:并成一段(取最小 start、最大 end),origin→edited。"""
    targets = [s for s in segments if s["seg_id"] in seg_ids]
    if len(targets) < 2:
        raise ValueError("merge 需至少两段")
    start = min(s["start_turn"] for s in targets)
    end = max(s["end_turn"] for s in targets)
    first = min(targets, key=lambda s: s["start_turn"])
    merged = dict(first)
    merged.update({
        "start_turn": start, "end_turn": end, "origin": "edited",
        "deletions": [d for s in sorted(targets, key=lambda x: x["start_turn"])
                      for d in s.get("deletions", [])],
        "covered_uuids": recompute_uuids(start, end, umap),
    })
    rest = [s for s in segments if s["seg_id"] not in seg_ids]
    rest.append(merged)
    return sorted(rest, key=lambda s: s["start_turn"])


def split(segments: list[dict], seg_id: str, at_turn: int, umap: dict[int, list[str]]) -> list[dict]:
    """在 at_turn 处把一段切两半:[start, at_turn] 与 [at_turn+1, end]。"""
    target = next((s for s in segments if s["seg_id"] == seg_id), None)
    if target is None:
        raise ValueError(f"无此段: {seg_id}")
    if not (target["start_turn"] <= at_turn < target["end_turn"]):
        raise ValueError(f"切点 {at_turn} 不在段 [{target['start_turn']},{target['end_turn']}) 内")
    left = dict(target)
    left.update({
        "end_turn": at_turn, "origin": "edited", "seg_id": "",
        "covered_uuids": recompute_uuids(target["start_turn"], at_turn, umap),
    })
    right = dict(target)
    right.update({
        "start_turn": at_turn + 1, "origin": "edited", "seg_id": "", "tag": "",
        "covered_uuids": recompute_uuids(at_turn + 1, target["end_turn"], umap),
    })
    rest = [s for s in segments if s["seg_id"] != seg_id]
    rest.extend([left, right])
    return sorted(rest, key=lambda s: s["start_turn"])


def set_boundary(
    segments: list[dict], seg_id: str, start_turn: int, end_turn: int, umap: dict[int, list[str]]
) -> list[dict]:
    """改一段的回合边界(移动段首/尾)。"""
    if start_turn > end_turn:
        raise ValueError("start_turn 不能大于 end_turn")
    out = []
    for s in segments:
        if s["seg_id"] == seg_id:
            s = dict(s)
            s.update({
                "start_turn": start_turn, "end_turn": end_turn, "origin": "edited",
                "covered_uuids": recompute_uuids(start_turn, end_turn, umap),
            })
        out.append(s)
    return sorted(out, key=lambda s: s["start_turn"])
