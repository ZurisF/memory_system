"""提取工作态持久化 —— home/staging/episodes/<session>.json。

S4 提取出的五件套是 S4→S5 之间的工作态,**不是正本**(正本是 active 碎片)。删了不
影响记忆;所以存可丢弃的 JSON,不进 DB、不进 fragments(守"碎片是正本、SQLite 可重建")。
S5 审核确认时才把它写成 active 碎片。

文档形态:
  { session_id, source_path, created_at, updated_at, seq,
    episodes: [ {stage_id, seg_id, start_turn, end_turn, status,
                 overview, summary, highlights, nodes, salience_tier, salience_reason,
                 source_text, deletions, origin, covered_uuids,
                 agent: {provider, model, extracted_at, usage, cost_usd, attempts}} ],
    retry: [ {seg_id, start_turn, end_turn, at, provider, model, errors} ] }

uuid(covered_uuids)只在本工作文件内部(供 S5 去重/溯源),不上 UI、不进碎片。
source_text 会上 UI(S5 审核/去噪要看原文),里面不含 uuid。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from memory_system.locks import lock_for


def _lock(session_id: str):
    """同一会话的读改写互斥。批量提取(逐段落盘)运行期间,用户对同会话
    confirm/edit/删条的写入才不会互相覆盖(server 多线程)。"""
    return lock_for(f"staging:{session_id}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path_for(staging_dir: Path, session_id: str) -> Path:
    safe = session_id.replace("/", "_").replace("..", "_")
    return staging_dir / f"{safe}.json"


def load(staging_dir: Path, session_id: str) -> dict | None:
    p = path_for(staging_dir, session_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _blank_doc(session_id: str, source_path: str) -> dict:
    return {
        "session_id": session_id,
        "source_path": source_path,
        "created_at": _now(),
        "updated_at": _now(),
        "seq": 0,
        "episodes": [],
        "retry": [],
    }


def _write(staging_dir: Path, doc: dict) -> dict:
    staging_dir.mkdir(parents=True, exist_ok=True)
    doc["updated_at"] = _now()
    p = path_for(staging_dir, doc["session_id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)  # 原子落盘
    return doc


def _episode_doc(doc: dict, seg: dict, result, source_text: str,
                 created_at: str | None = None) -> dict:
    """把一段提取结果组装成 staging episode(stage_id 沿用同 seg_id 的旧值,否则新分配)。

    created_at = 段首回合的对话发生时间(归档成 active 碎片时的 created_at)。transcript 约 30 天
    会被清理,故归档素材必须在 staging 内自洽,此处就把发生时间存下,确认时不再回读 transcript。
    """
    seg_id = seg.get("seg_id") or ""
    prev = next((e for e in doc.get("episodes", []) if e.get("seg_id") == seg_id), None)
    if prev and prev.get("stage_id"):
        stage_id = prev["stage_id"]
    else:
        seq = int(doc.get("seq", 0)) + 1
        doc["seq"] = seq
        stage_id = f"e{seq}"
    return {
        "stage_id": stage_id,
        "seg_id": seg_id,
        "start_turn": seg.get("start_turn"),
        "end_turn": seg.get("end_turn"),
        "created_at": created_at,
        "status": "staging",
        "overview": result.overview,
        "summary": result.summary,
        "highlights": result.highlights,
        "nodes": result.nodes,
        "salience_tier": result.salience_tier,
        "salience_reason": result.salience_reason,
        "source_text": source_text,
        # 切块标的可删内容随段带过来,供 S5 审核界面人工去噪参考(本步不物理删除)。
        "deletions": list(seg.get("deletions") or []),
        "origin": "agent",
        "covered_uuids": list(seg.get("covered_uuids") or []),
        "agent": {
            "provider": result.provider,
            "model": result.model,
            "extracted_at": _now(),
            "usage": result.usage,
            "cost_usd": result.cost_usd,
            "attempts": result.attempts,
        },
    }


def upsert_episode(
    staging_dir: Path,
    session_id: str,
    source_path: str,
    seg: dict,
    result,  # extract.ExtractResult
    source_text: str,
    created_at: str | None = None,
) -> dict:
    """落一段提取结果(按 seg_id upsert:重提取覆盖旧的)。成功即清掉该段的 retry 记录。

    created_at = 段首回合发生时间,随段存入 staging(归档时作 episode 的 created_at)。
    """
    with _lock(session_id):
        doc = load(staging_dir, session_id) or _blank_doc(session_id, source_path)
        doc["source_path"] = source_path
        ep = _episode_doc(doc, seg, result, source_text, created_at)
        eps = [e for e in doc.get("episodes", []) if e.get("seg_id") != ep["seg_id"]]
        eps.append(ep)
        doc["episodes"] = sorted(eps, key=lambda e: (e.get("start_turn") or 0))
        # 这段提取成功 → 移除它之前的失败记录(retry 列表只留当前仍坏的段)
        doc["retry"] = [r for r in doc.get("retry", []) if r.get("seg_id") != ep["seg_id"]]
        return _write(staging_dir, doc)


def get_episode(staging_dir: Path, session_id: str, stage_id: str) -> dict | None:
    """按 stage_id 取一条 staging episode(不存在 → None)。"""
    doc = load(staging_dir, session_id)
    if not doc:
        return None
    return next((e for e in doc.get("episodes", []) if e.get("stage_id") == stage_id), None)


# S5 可编辑字段:五件套 + 人工去噪后的 source_text(uuid/工作态字段不可改)。
_EDITABLE = {"overview", "summary", "highlights", "nodes", "salience_tier",
             "salience_reason", "source_text", "deletions"}


def edit_episode(staging_dir: Path, session_id: str, stage_id: str, fields: dict) -> dict:
    """人工编辑一条 staging episode 的五件套 / 去噪后 source_text(origin→edited),落盘。

    确认前的修改全停在 staging,不碰正本;只允许改 _EDITABLE 字段,covered_uuids 等工作态不动。
    """
    with _lock(session_id):
        doc = load(staging_dir, session_id)
        if not doc:
            raise KeyError(f"无 staging 文档: {session_id}")
        ep = next((e for e in doc.get("episodes", []) if e.get("stage_id") == stage_id), None)
        if ep is None:
            raise KeyError(f"staging 无此 episode: {stage_id}")
        for k, v in fields.items():
            if k in _EDITABLE:
                ep[k] = v
        ep["origin"] = "edited"
        return _write(staging_dir, doc)


def remove_episode(staging_dir: Path, session_id: str, stage_id: str) -> dict:
    """从 staging 移除一条 episode(确认归档后工作态消费完)。"""
    with _lock(session_id):
        doc = load(staging_dir, session_id)
        if not doc:
            raise KeyError(f"无 staging 文档: {session_id}")
        doc["episodes"] = [e for e in doc.get("episodes", []) if e.get("stage_id") != stage_id]
        return _write(staging_dir, doc)


def reject_episode(staging_dir: Path, session_id: str, stage_id: str,
                   reason: str | None = None) -> dict:
    """拒一条 staging episode:从 episodes 移除,留痕到 rejected 列表(不写碎片不进 DB)。"""
    with _lock(session_id):
        doc = load(staging_dir, session_id)
        if not doc:
            raise KeyError(f"无 staging 文档: {session_id}")
        ep = next((e for e in doc.get("episodes", []) if e.get("stage_id") == stage_id), None)
        if ep is None:
            raise KeyError(f"staging 无此 episode: {stage_id}")
        doc["episodes"] = [e for e in doc["episodes"] if e.get("stage_id") != stage_id]
        doc.setdefault("rejected", []).append({
            "stage_id": stage_id, "seg_id": ep.get("seg_id"),
            "start_turn": ep.get("start_turn"), "end_turn": ep.get("end_turn"),
            "rejected_at": _now(), "reason": reason,
        })
        return _write(staging_dir, doc)


def clear_retry(staging_dir: Path, session_id: str, seg_ids: list[str]) -> dict | None:
    """手动忽略失败记录:按 seg_id 从 retry 列表移除(不动 episodes、不留痕)。

    给 UI「关闭失败卡」用——人工判定这段不必再提取/重试,把告警清掉。
    文档不存在或无 retry 变化 → 直接返回当前(或 None)。
    """
    with _lock(session_id):
        doc = load(staging_dir, session_id)
        if not doc:
            return None
        targets = set(seg_ids or [])
        kept = [r for r in doc.get("retry", []) if r.get("seg_id") not in targets]
        if len(kept) == len(doc.get("retry", [])):
            return doc  # 无变化,不必落盘
        doc["retry"] = kept
        return _write(staging_dir, doc)


def append_retry(
    staging_dir: Path,
    session_id: str,
    source_path: str,
    seg: dict,
    *,
    provider: str,
    model: str,
    errors: list[str],
) -> dict:
    """记一段提取失败(供 UI 告警 + 人工重试)。同段只留最新一条;不动已 staged 的段。"""
    with _lock(session_id):
        return _append_retry_locked(staging_dir, session_id, source_path, seg,
                                    provider=provider, model=model, errors=errors)


def _append_retry_locked(
    staging_dir: Path,
    session_id: str,
    source_path: str,
    seg: dict,
    *,
    provider: str,
    model: str,
    errors: list[str],
) -> dict:
    doc = load(staging_dir, session_id) or _blank_doc(session_id, source_path)
    doc["source_path"] = source_path
    seg_id = seg.get("seg_id") or ""
    doc["retry"] = [r for r in doc.get("retry", []) if r.get("seg_id") != seg_id]
    doc["retry"].append({
        "seg_id": seg_id,
        "start_turn": seg.get("start_turn"),
        "end_turn": seg.get("end_turn"),
        "at": _now(),
        "provider": provider,
        "model": model,
        "errors": list(errors),
    })
    return _write(staging_dir, doc)
