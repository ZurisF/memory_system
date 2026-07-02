"""编辑写回 —— 改 active/archived 碎片正本的「正文四件」+ 增量同步 SQLite。

可编辑范围(Phase 1 锁定):`overview` / `summary` / `highlights` / `salience_tier`。
**不可编辑**:`source_text`(逐字原文正本,改它破坏不变量)、`nodes`(膜/概念图编辑留后)、
public_id/status/时间戳等身份与生命周期字段。

落地顺序硬约束(与 `archive.confirm_episode` 同philosophy):所有可失败动作(重嵌 embedding、
向量写、DB 约束)都在事务内 commit 成功**之后**,才回写碎片正本。任一步失败 → 回滚、碎片原封
不动、可干净重试。

**重嵌只在 overview 真变时做**(它是唯一进向量的字段):省额度、省网络。summary/highlights/
salience_tier 改了不联网。source_text 不变,FTS 由 `episodes_au` 触发器重灌同内容,无副作用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlite_vec

from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.embedding.base import EmbeddingProvider
from memory_system.fragments import episode_path, read_episode, write_episode
from memory_system.index import assert_embeddable

# 可编辑字段白名单(正文四件)。传入此外的键即报错——明确告诉调用方 source_text/nodes 不可改。
EDITABLE = ("overview", "summary", "highlights", "salience_tier")


class EditError(RuntimeError):
    """编辑写回的可预期失败(无碎片、无 DB 索引、字段非法、向量维度不符等)。"""


@dataclass
class EditReport:
    public_id: str
    changed: list[str]      # 实际发生变化的字段
    reembedded: bool        # overview 是否变化并触发重嵌


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_highlights(raw: object) -> list[dict]:
    """规整 highlights:list[{text(非空), tag}],至多 3 条。坏即抛 EditError。"""
    if not isinstance(raw, list):
        raise EditError("highlights 必须是数组")
    if len(raw) > 3:
        raise EditError(f"highlights 至多 3 条,收到 {len(raw)}")
    out: list[dict] = []
    for i, hl in enumerate(raw):
        if not isinstance(hl, dict):
            raise EditError(f"highlights[{i}] 必须是对象")
        text = str(hl.get("text", ""))
        if not text.strip():
            raise EditError(f"highlights[{i}] 的 text 不能为空")
        # text 逐字保留、不 strip(与 extract._validate_highlights 一致):
        # highlight 是逐字原话,首尾空白可能就是原文的一部分。
        out.append({"text": text, "tag": str(hl.get("tag", "")).strip()})
    return out


def edit_episode(
    cfg: Config, public_id: str, fields: dict, emb_provider: EmbeddingProvider
) -> EditReport:
    """编辑一条 episode 的正文四件,写回碎片正本 + 增量同步 DB。返回 EditReport。

    fields 只允许 EDITABLE 内的键(传别的即报错)。overview 变化才重嵌并改向量。
    """
    if not isinstance(fields, dict) or not fields:
        raise EditError("缺 fields")
    unknown = [k for k in fields if k not in EDITABLE]
    if unknown:
        raise EditError(f"字段不可编辑: {unknown}(可编辑: {list(EDITABLE)};source_text/nodes 不可改)")

    p = episode_path(cfg.episodes_dir, public_id)
    if not p.exists():
        raise EditError(f"无此碎片: {public_id}")
    ep = read_episode(p)

    # ---- 计算新值(合并到当前态)+ 校验 ----
    new_overview = ep.overview
    if "overview" in fields:
        new_overview = str(fields["overview"]).strip()
        if not new_overview:
            raise EditError("overview 不能为空")
    new_summary = ep.summary
    if "summary" in fields:
        new_summary = str(fields["summary"]).strip()
        if not new_summary:
            raise EditError("summary 不能为空")
    new_highlights = ep.highlights
    if "highlights" in fields:
        new_highlights = _validate_highlights(fields["highlights"])
    new_tier = ep.salience_tier
    if "salience_tier" in fields:
        try:
            new_tier = int(fields["salience_tier"])
        except (TypeError, ValueError):
            raise EditError("salience_tier 必须是整数") from None
        if new_tier not in (1, 2, 3):
            raise EditError(f"salience_tier 须 ∈ 1,2,3,收到 {new_tier}")

    changed: list[str] = []
    if new_overview != ep.overview:
        changed.append("overview")
    if new_summary != ep.summary:
        changed.append("summary")
    if new_highlights != ep.highlights:
        changed.append("highlights")
    if new_tier != ep.salience_tier:
        changed.append("salience_tier")
    if not changed:
        return EditReport(public_id=public_id, changed=[], reembedded=False)

    reembed = "overview" in changed

    # ---- DB 增量同步:可失败动作(重嵌/向量/约束)在 commit 之前,成功后才回写碎片 ----
    con = connect(cfg.db_path)
    try:
        migrate.up(con)
        row = con.execute("SELECT id FROM episodes WHERE public_id=?", (public_id,)).fetchone()
        if not row:
            raise EditError(f"DB 无此 episode 索引: {public_id};请先 `index rebuild`")
        eid = row[0]

        vec = None
        if reembed:
            model, dim = assert_embeddable(con, emb_provider)
            vec = emb_provider.embed([new_overview])[0]
            if len(vec) != dim:
                raise EditError(f"overview 向量维度 {len(vec)} ≠ meta 锁 {dim},拒写")

        con.execute(
            "UPDATE episodes SET overview=?, summary=?, highlights_json=?, salience_tier=? WHERE id=?",
            (
                new_overview,
                new_summary,
                json.dumps(new_highlights, ensure_ascii=False) if new_highlights else None,
                new_tier,
                eid,
            ),
        )
        if reembed:
            con.execute("DELETE FROM episode_vectors WHERE episode_id=?", (eid,))  # vec0 改值=删后插
            con.execute(
                "INSERT INTO episode_vectors(episode_id,embedding) VALUES(?,?)",
                (eid, sqlite_vec.serialize_float32(vec)),
            )
            con.execute("UPDATE episodes SET last_embedded_at=? WHERE id=?", (_now(), eid))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    # DB 落定,才回写碎片正本(source_text/nodes/身份字段原样保留)
    ep.overview = new_overview
    ep.summary = new_summary
    ep.highlights = new_highlights
    ep.salience_tier = new_tier
    write_episode(cfg.episodes_dir, ep)
    return EditReport(public_id=public_id, changed=changed, reembedded=reembed)
