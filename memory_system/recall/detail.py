"""细节检索(S6-2):FTS 全文 grep + 开窗。

三个 tool 里最简单:无 embedding、无 LLM、无衰减(grep 层整层豁免衰减排序)。
- FTS5 MATCH `episode_fts`,join 回 `episodes`,硬过滤 `status='active'`。
- 排序:bm25(FTS5 内建 rank),同分按 `created_at` 降序。取 `detail_limit` 条。
- `--since/--until` 按 `episodes.created_at` ISO 串比较过滤。
- 默认开窗 `snippet(...)`;`--raw` 返回整条 source_text(逐字保真,不接重构)。
- 命中即刷新命中 episode 的时钟(细节检索是主动命中),刷完 commit。
- 中文短词(<3 字)trigram 不可靠:FTS 空手(含 OperationalError)时走 instr 子串回退
  (S6.5)——按出现次数降序、created_at 降序,Python 侧开窗,时钟/契约与 FTS 路一致。
  ≥3 字空手不回退:那是真没有,避免长 query 改性。

红线:返回 dict 手工挑字段,对外只用 public_id;绝无 uuid / 向量 / DB 整数 id。
(内部 id 只用于时钟 UPDATE,不进返回。)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.recall import decay


def _fts_phrase(query: str) -> str:
    """把用户 query 包成单个 FTS5 短语(双引号内成字面量),中和 MATCH 语法字符。

    细节检索是 grep 语义:trigram + 短语 = 子串匹配,正合"逐字找原文"的意图。
    内部双引号按 FTS5 规则双写转义。
    """
    return '"' + query.replace('"', '""') + '"'


def _substr_window(text: str, q: str, window: int) -> str:
    """instr 回退的 Python 侧开窗:首次出现位置前后各 window 字符,截断端加 …。

    与 FTS snippet 的意图对齐(命中词带上下文);--raw 不走这里,仍整条原文。
    """
    pos = text.find(q)
    if pos < 0:  # instr 已保证命中;纯防御
        return text
    start = max(0, pos - window)
    end = min(len(text), pos + len(q) + window)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def recall_detail(
    cfg: Config,
    query: str,
    *,
    since: str | None = None,
    until: str | None = None,
    raw: bool = False,
    limit: int | None = None,
    touch: bool = True,
    now: datetime | None = None,
) -> dict:
    """FTS 检索 active episode。返回 §5 detail 契约:{mode, query, hits:[...]}。

    中文短词(<3 字)FTS 空手时自动走 instr 子串回退(S6.5,见模块 docstring)。
    `touch=False` 供 eval/只读场景关掉时钟刷新(§S6-8);默认命中即刷,回退路同规则。
    """
    rc = cfg.recall
    lim = limit if (limit and limit > 0) else rc.detail_limit
    now = now or datetime.now(timezone.utc)

    hits: list[dict] = []
    q = (query or "").strip()
    if not q:
        return {"mode": "detail", "query": query, "hits": hits}

    con = connect(cfg.db_path)
    try:
        migrate.up(con)  # 防御:检索前确保 schema 就位(与 views 一致)
        where = ["episode_fts MATCH ?", "e.status='active'"]
        params: list = [_fts_phrase(q)]
        if since:
            where.append("e.created_at >= ?")
            params.append(since)
        if until:
            where.append("e.created_at <= ?")
            params.append(until)
        sql = f"""
            SELECT e.id, e.public_id, e.created_at, e.salience_tier, e.source_text,
                   snippet(episode_fts, 0, '', '', '…', ?) AS window
            FROM episode_fts JOIN episodes e ON e.id = episode_fts.rowid
            WHERE {' AND '.join(where)}
            ORDER BY bm25(episode_fts), e.created_at DESC
            LIMIT ?
        """
        try:
            rows = con.execute(sql, [rc.window_tokens, *params, lim]).fetchall()
        except sqlite3.OperationalError:
            # query 全是标点/无有效 trigram → FTS 报错,当作未命中(短词走下方回退)。
            rows = []

        # S6.5:中文短词(<3 字)FTS 空手 → instr 子串回退。detail 本就是 grep 语义,
        # 子串扫描不改性;≥3 字空手不回退(真没有,交 CLI 提示)。
        fallback = not rows and len(q) < 3
        if fallback:
            fb_where = ["e.status='active'", "instr(e.source_text, ?) > 0"]
            # 占位符按 SQL 文本序:occ 表达式两个 → WHERE instr 一个 → since/until → LIMIT
            fb_params: list = [q, q, q]
            if since:
                fb_where.append("e.created_at >= ?")
                fb_params.append(since)
            if until:
                fb_where.append("e.created_at <= ?")
                fb_params.append(until)
            fb_sql = f"""
                SELECT e.id, e.public_id, e.created_at, e.salience_tier, e.source_text,
                       (length(e.source_text) - length(replace(e.source_text, ?, '')))
                           / length(?) AS occ
                FROM episodes e
                WHERE {' AND '.join(fb_where)}
                ORDER BY occ DESC, e.created_at DESC
                LIMIT ?
            """
            rows = con.execute(fb_sql, [*fb_params, lim]).fetchall()

        hit_ids: list[int] = []
        for r in rows:
            hit_ids.append(r["id"])
            if raw:
                window = r["source_text"]
            elif fallback:
                window = _substr_window(r["source_text"], q, rc.window_tokens)
            else:
                window = r["window"]
            hits.append({
                "public_id": r["public_id"],
                "window": window,
                "created_at": r["created_at"],
                "salience_tier": r["salience_tier"],
            })

        if touch and hit_ids:
            decay.touch_episodes(con, hit_ids, now)
            con.commit()
    finally:
        con.close()

    return {"mode": "detail", "query": query, "hits": hits}
