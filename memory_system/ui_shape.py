"""送前端的形状裁剪 —— 铁律 2「uuid / 向量永不上台面」的单一执行点。

工作态 JSON(segments / staging episodes)里带 `covered_uuids`(uuid 只允许活在工作态/
书签),送浏览器前一律经这里剥掉。HTTP 层只编排,不自己拼形状。
"""

from __future__ import annotations


def ui_segment(s: dict) -> dict:
    """送前端的段:剥掉 covered_uuids(uuid 不上台面)。"""
    return {k: v for k, v in s.items() if k != "covered_uuids"}


def ui_episode(e: dict) -> dict:
    """送前端的 staging episode:剥掉 covered_uuids。source_text 保留(S5 审核要看)。"""
    return {k: v for k, v in e.items() if k != "covered_uuids"}


def ui_staging(doc: dict | None) -> dict:
    if not doc:
        return {"episodes": [], "retry": [], "updated_at": None}
    return {
        "episodes": [ui_episode(e) for e in doc.get("episodes", [])],
        "retry": doc.get("retry", []),
        "updated_at": doc.get("updated_at"),
    }


def ui_doc(doc: dict | None) -> dict:
    if not doc:
        return {"segments": [], "agent": None, "retry": [], "source_mtime": None}
    return {
        "segments": [ui_segment(s) for s in doc.get("segments", [])],
        "agent": doc.get("agent"),
        "retry": doc.get("retry", []),
        "source_mtime": doc.get("source_mtime"),
        "updated_at": doc.get("updated_at"),
    }
