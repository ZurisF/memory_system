"""开场注入(S6-6):选材 + 重构 + cache 读写 + dirty 标记。

s6_build_plan §4 S6-6 的要点:
- **选材是填槽不是排序**:
  槽 A(latest)= created_at 最新的 1 条 active(接上"上次聊到");
  槽 B(ballast)= salience_tier >= 2 中 effective_activation 最高的 1–2 条(压舱),
  去掉与槽 A 重复的;槽 C(spark 火花)= 全部 active 减 A/B 后温度采样(Phase 2)。
  硬顶 `opening_max_items` 条:spark 开启时先给槽 C 保留席位,槽 B 取剩余预算;
  `opening_spark=0` 完全回归 Phase 1(槽 C 空着)。
- **槽 C 权重 = 重要且沉睡**(§0.7):`w = salience_tier ×(1 − activation)+ 0.05`;
  温度采样 `p ∝ w^(1/T)`(T→0 贪心、T 大趋均匀),不放回采 `opening_spark` 条。
  随机性是特性(serendipity),函数收 `rng` 形参供 verify 注入固定 seed;生产路径用系统熵。
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


def _spark_weight(activation: float, tier: int) -> float:
    """槽 C 采样权重:重要且沉睡(§0.7)= salience_tier ×(1 − activation)+ 0.05。
    +0.05 保证任何候选都有正权重(不会被彻底剪除),恒 > 0。"""
    return tier * (1.0 - activation) + 0.05


def _sample_spark(cands: list, weights: list, k: int, temp: float, rng) -> list:
    """按 `p ∝ w^(1/T)` 不放回采 k 条,返回选中行(采样顺序)。

    - `temp <= 0`:退化为贪心(温度趋 0 的极限),按权重降序、public_id 破平,完全确定;
    - 否则先对 `w / w_max` 取 `1/T` 次幂(归一化防溢出,常数因子不改相对概率),再轮盘赌。
    cands 须已按 public_id 稳定排序 —— 同 seed 同库必得同一结果(verify 断言确定性)。
    """
    pool = list(zip(cands, weights))
    k = min(k, len(pool))
    if k <= 0:
        return []
    if temp <= 0:  # 贪心极限:直接按权重降序取,不消耗 rng
        pool.sort(key=lambda t: (-t[1], t[0]["public_id"]))
        return [row for row, _ in pool[:k]]
    wmax = max(w for _, w in pool) or 1.0
    pw = [(w / wmax) ** (1.0 / temp) for _, w in pool]
    idxs = list(range(len(pool)))
    chosen: list = []
    for _ in range(k):
        total = sum(pw[i] for i in idxs)
        if total <= 0:  # 防御:全零权重(理论不达)→ 均匀挑
            pick = rng.randrange(len(idxs))
        else:
            r = rng.random() * total
            acc = 0.0
            pick = len(idxs) - 1
            for j, i in enumerate(idxs):
                acc += pw[i]
                if r <= acc:
                    pick = j
                    break
        chosen.append(pool[idxs[pick]][0])
        idxs.pop(pick)
    return chosen


def select_opening(cfg: Config, *, now: datetime | None = None, rng=None) -> dict:
    """选材:槽 A(latest)+ 槽 B(ballast)+ 槽 C(spark 火花)。**全程只 SELECT,不刷时钟**。

    返回结构化 dict(喂重构,槽位即候选集,可日志可重放):
    {"mode": "opening", "token_budget": N,
     "slots": {"latest": [...], "ballast": [...], "spark": [...]}}
    `rng`(None → random.Random())供 verify 注入固定 seed;生产路径用系统熵。
    """
    rc = cfg.recall
    now = now or datetime.now(timezone.utc)
    if rng is None:
        import random  # 轻依赖,函数内 import(仅默认构造用;签名注解已 future-stringify)
        rng = random.Random()
    con = connect(cfg.db_path)
    try:
        migrate.up(con)  # 防御:检索前确保 schema 就位(与其余 recall 模块一致)
        rows = con.execute(
            """SELECT public_id, overview, summary, highlights_json, created_at,
                      salience_tier, last_accessed_at, activated_at
               FROM episodes WHERE status='active'""").fetchall()
    finally:
        con.close()

    slots: dict = {"latest": [], "ballast": [], "spark": []}
    out = {"mode": "opening", "token_budget": rc.opening_token_budget, "slots": slots}
    if not rows or rc.opening_max_items < 1:
        return out

    def _act(r) -> float:
        return decay.effective_activation(
            r["last_accessed_at"], r["salience_tier"], rc, now,
            activated_at=r["activated_at"], created_at=r["created_at"])

    # 槽 A:created_at 最新的 1 条(同刻并列按 public_id 定序,保证可重放)
    latest = max(rows, key=lambda r: (r["created_at"], r["public_id"]))
    slots["latest"].append(_item(latest))
    taken = {latest["public_id"]}

    # 槽位预算(§0.9):硬顶 opening_max_items 不变;spark 开启先给槽 C 保留席位,槽 B 取剩余。
    remaining = rc.opening_max_items - len(slots["latest"])
    spark_reserved = min(max(rc.opening_spark, 0), remaining)
    ballast_n = min(2, remaining - spark_reserved)

    # 槽 B:tier >= 2 中活跃度最高的 1–2 条,去掉与槽 A 重复的
    if ballast_n > 0:
        cands = [r for r in rows
                 if r["salience_tier"] >= 2 and r["public_id"] not in taken]
        act = {r["public_id"]: _act(r) for r in cands}
        cands.sort(key=lambda r: (-act[r["public_id"]], r["public_id"]))
        for r in cands[:ballast_n]:
            slots["ballast"].append(_item(r, activation=act[r["public_id"]]))
            taken.add(r["public_id"])

    # 槽 C(spark):全部 active − 槽 A/B 已选;权重=重要且沉睡,温度采样不放回。
    if spark_reserved > 0:
        sc = sorted((r for r in rows if r["public_id"] not in taken),
                    key=lambda r: r["public_id"])  # 稳定序 → 同 seed 结果确定
        if sc:
            sact = {r["public_id"]: _act(r) for r in sc}
            sw = [_spark_weight(sact[r["public_id"]], r["salience_tier"]) for r in sc]
            picks = _sample_spark(sc, sw, spark_reserved, rc.opening_spark_temp, rng)
            slots["spark"].extend(_item(r, activation=sact[r["public_id"]]) for r in picks)
            # 可重放日志:槽 C 的 public_id 与其权重(选材侧独立落盘,重放可复现)。
            wmap = {r["public_id"]: w for r, w in zip(sc, sw)}
            get_logger().info(
                "opening 槽 C 火花采样(可重放): temp=%s picks=%s",
                rc.opening_spark_temp,
                [{"public_id": p["public_id"], "weight": round(wmap[p["public_id"]], 6)}
                 for p in picks])
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
