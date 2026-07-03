"""开场注入(S6-6):选材 + 重构 + cache 读写 + dirty 标记。

s6_build_plan §4 S6-6 的要点:
- **选材是填槽不是排序**:
  槽 A(latest)= created_at 最新的 1 条 active(接上"上次聊到");
  槽 B(ballast)= salience_tier >= 2 中 effective_activation 最高的 1–2 条(压舱),
  去掉与槽 A 重复的;槽 C(温度采样火花)Phase 1 空着不建。硬顶 `opening_max_items` 条。
- **只读窥视:选材全程不刷新任何时钟**(§6.3 裁定:开场注入是窥视不是回忆,
  刷新规则四条里它是"全不刷"的那条)。本模块对 DB 只 SELECT,绝无 touch/UPDATE。
- 重构复用 reconstruct.run(mode="opening",prompt 在 prompts/opening_system.txt),
  预算 `opening_token_budget`(随结构化输入送进 prompt);产物原子写
  `opening_cache/global.md`(tmp + os.replace,照 fragments._atomic_write_text 惯例)。
- **dirty 机制**:active 集变动的四个写入点(confirm / archive / delete episode / 编辑写回)
  各调一行 `mark_dirty(cfg)` = touch `opening_cache/.dirty`;`rebuild_opening` 默认只在
  dirty 存在时重建(force 无视),成功后删 dirty。重构失败时 dirty 保留、cache 不动,下次可重试。

SessionStart hook 只读 cache 文件(毫秒级),hook 接线在仓库外,施工到 `opening show` 为止。
红线:结构化选材手工挑字段,对外只 public_id,无 uuid / 向量 / DB 整数 id;
概念层纪律同样适用——开场只用 overview/summary/highlights,不带 source_text(预算也装不下)。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.log import get_logger
from memory_system.recall import decay

# 开场没有"用户当轮 query"——三部分输入铁律(§0.2)保形,此固定说明占第三部分。
_OPENING_QUERY = "(新会话开场注入:无当轮 query。请按系统提示把选材揉成一段开场回忆。)"

# 空库时的占位文案:不调 LLM(没有任何候选可"表达"),写占位保证 `show` 有物可读。
_EMPTY_TEXT = "(记忆库暂无 active 记忆,无可注入的开场。)"


# ---- 路径与 dirty 标记 ----


def cache_path(cfg: Config) -> Path:
    return cfg.opening_cache_dir / "global.md"


def _dirty_path(cfg: Config) -> Path:
    return cfg.opening_cache_dir / ".dirty"


def mark_dirty(cfg: Config) -> None:
    """标记开场 cache 过期(active 集变动的四个写入点各调一行)。

    best-effort:目录缺失就补建(老主目录未重 init 也不炸);标记失败只记日志,
    绝不让 dirty 标记拖垮 confirm/删除/编辑这些正经写入动作。
    """
    try:
        cfg.opening_cache_dir.mkdir(parents=True, exist_ok=True)
        _dirty_path(cfg).touch()
    except OSError as e:  # noqa: BLE001
        get_logger().warning("opening mark_dirty 失败(忽略,不拖垮写入侧): %s", e)


def is_dirty(cfg: Config) -> bool:
    return _dirty_path(cfg).exists()


def clear_dirty(cfg: Config) -> None:
    _dirty_path(cfg).unlink(missing_ok=True)


def _atomic_write_text(p: Path, text: str) -> None:
    """tmp + os.replace 原子替换(照 fragments._atomic_write_text 惯例):
    SessionStart hook 随时在读这个文件,绝不能让它读到半截。tmp 与目标同目录。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


# ---- 选材(填槽非排序;只读窥视,不刷任何时钟)----


def _item(row, *, activation: float | None = None) -> dict:
    """手工挑字段(红线):只 public_id + 内容字段,无 uuid / 向量 / DB 整数 id / source_text。"""
    import json

    out = {
        "public_id": row["public_id"],
        "overview": row["overview"],
        "summary": row["summary"],
        "highlights": json.loads(row["highlights_json"]) if row["highlights_json"] else [],
        "created_at": row["created_at"],
        "salience_tier": row["salience_tier"],
    }
    if activation is not None:
        out["activation"] = round(activation, 6)
    return out


def select_opening(cfg: Config, *, now: datetime | None = None) -> dict:
    """选材:槽 A(latest)+ 槽 B(ballast),槽 C Phase 1 空着。**全程只 SELECT,不刷时钟**。

    返回结构化 dict(喂重构,槽位即候选集,可日志可重放):
    {"mode": "opening", "token_budget": N, "slots": {"latest": [...], "ballast": [...]}}
    """
    rc = cfg.recall
    now = now or datetime.now(timezone.utc)
    con = connect(cfg.db_path)
    try:
        migrate.up(con)  # 防御:检索前确保 schema 就位(与其余 recall 模块一致)
        rows = con.execute(
            """SELECT public_id, overview, summary, highlights_json, created_at,
                      salience_tier, last_accessed_at, activated_at
               FROM episodes WHERE status='active'""").fetchall()
    finally:
        con.close()

    slots: dict = {"latest": [], "ballast": []}
    out = {"mode": "opening", "token_budget": rc.opening_token_budget, "slots": slots}
    if not rows or rc.opening_max_items < 1:
        return out

    # 槽 A:created_at 最新的 1 条(同刻并列按 public_id 定序,保证可重放)
    latest = max(rows, key=lambda r: (r["created_at"], r["public_id"]))
    slots["latest"].append(_item(latest))

    # 槽 B:tier >= 2 中活跃度最高的 1–2 条,去掉与槽 A 重复的;硬顶 opening_max_items
    ballast_n = min(2, rc.opening_max_items - len(slots["latest"]))
    if ballast_n > 0:
        cands = [r for r in rows
                 if r["salience_tier"] >= 2 and r["public_id"] != latest["public_id"]]
        act = {r["public_id"]: decay.effective_activation(
            r["last_accessed_at"], r["salience_tier"], rc, now,
            activated_at=r["activated_at"], created_at=r["created_at"]) for r in cands}
        cands.sort(key=lambda r: (-act[r["public_id"]], r["public_id"]))
        slots["ballast"].extend(_item(r, activation=act[r["public_id"]])
                                for r in cands[:ballast_n])
    return out


# ---- rebuild:选材 → 重构 → 原子写 cache → 清 dirty ----


def rebuild_opening(
    cfg: Config,
    *,
    force: bool = False,
    provider=None,
    now: datetime | None = None,
) -> str | None:
    """重建开场 cache。默认只在 .dirty 存在时重建(force 无视);跳过时返回 None。

    成功:原子写 `opening_cache/global.md`、删 .dirty、返回写入文本。
    重构失败抛 ChatError(调用方决定退出码):cache 不动、dirty 保留,下次 rebuild 重试。
    provider 形参供测试注入;默认走 reconstruct 内部的 get_chat_provider(cfg.agent)。
    """
    if not force and not is_dirty(cfg):
        return None
    structured = select_opening(cfg, now=now)
    if not structured["slots"]["latest"] and not structured["slots"]["ballast"]:
        text = _EMPTY_TEXT  # 空库:无候选可表达,不调 LLM
    else:
        # 重依赖(agent 工厂)函数内 import:写入侧只为 mark_dirty import 本模块,保持轻。
        from memory_system.recall import reconstruct

        text = reconstruct.run(cfg, "opening", structured, _OPENING_QUERY, provider=provider)
    _atomic_write_text(cache_path(cfg), text + ("\n" if not text.endswith("\n") else ""))
    clear_dirty(cfg)
    return text
