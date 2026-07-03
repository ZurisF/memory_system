"""S6 检索层 headless 验收(fake embedding + 临时 home,离线跑)。

按 s6_build_plan §4 增量生长后已收口成型:一条链从空库一键全绿——
临时 home → all_dirs 建目录 → 写碎片 → index rebuild(建 DB/向量/FTS/膜)→
三路检索(细节/情景/概念)→ 衰减/时钟规则 → 重构管线(fake chat)→ 开场注入。
覆盖 S6-1..S6-6 六步;跑两遍幂等(每步自带干净基线,rebuild 重置运行态时钟属预期)。

- S6-1:半衰期数学(tier=1/14 天≈0.5、elapsed=0→1.0、tier=3/14 天>0.9)、NULL 回退链、
        touch 后活跃度回 1.0。
- S6-2:精确词命中且窗口含该词、未命中空且退出码 0、--since 排除窗外条目、
        命中刷时钟未命中不动、红线(--json 无 uuid / 无 embedding)。
- S6-3:双路命中 RRF 分高于单路、三槽正确性(主槽条数/同源同 session 紧邻/联想 via_nodes
        不重复)、时钟只刷 top-1+同源联想不刷、FTS 空手不崩单路照常、红线同前。
- S6-4:label 直查/alias 查带 bridge/miss 报错三路径、概念层无 source_text、
        node 时钟刷新 episode 时钟未动、--context 相似度排序。
- S6-5:默认路径走重构(输出=fake 返回值)、--raw/--json/detail 不调 chat、
        ChatError 降级回结构化退出码非零、日志有候选集记录(可重放)。
- S6-6:选材填槽(槽 A 最新条 / 槽 B tier>=2 压舱 / 硬顶去重)、只读窥视不刷任何时钟、
        三部分输入重构、dirty 门(无 dirty 跳过、--force 强跑)、CLI show/rebuild、
        写入侧 confirm 接线冒出 .dirty、红线(选材无 uuid/embedding/DB id/source_text)。

跑法:.venv/bin/python scripts/verify_s6.py
(评测夹具 eval/queries.jsonl + scripts/eval_recall.py 对真实库跑,不进本回归。)
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="memsys_s6_")
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"
os.environ["MEMORY_AGENT_PROVIDER"] = "fake"

from datetime import datetime, timezone  # noqa: E402

from memory_system.config import load_config  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.embedding.fake import FakeProvider  # noqa: E402
from memory_system.fragments import Episode, write_episode  # noqa: E402
from memory_system.index import rebuild  # noqa: E402
from memory_system.recall import decay, recall_detail  # noqa: E402

CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)
print(f"临时 home: {_TMP}")

# 固定"现在",让半衰期数学可断言(2026-06-18 = 14 天前)。
NOW = datetime(2026, 7, 2, 0, 0, 0, tzinfo=timezone.utc)
D14 = "2026-06-18T00:00:00+00:00"   # NOW - 14 天
OLD = "2026-01-01T00:00:00+00:00"

# 细节检索语料:3 条含已知原文的 active episode。
#  A/C 共享词"记忆系统"(测多命中 + --since 过滤);B 只含"蓝莓松饼"(测精确命中/未命中不动)。
EP_A = Episode(public_id="ep_aaaa0001", overview="量子与记忆概览",
               summary="A 摘要", source_text="今天讨论了量子纠缠,也顺便聊了记忆系统的设计。",
               salience_tier=2, status="active", created_at="2026-06-01T09:00:00+00:00",
               activated_at="2026-06-01T09:00:00+00:00")
EP_B = Episode(public_id="ep_bbbb0002", overview="蓝莓松饼概览",
               summary="B 摘要", source_text="周末做了蓝莓松饼当早餐,味道很好。",
               salience_tier=1, status="active", created_at="2026-06-15T09:00:00+00:00",
               activated_at="2026-06-15T09:00:00+00:00")
EP_C = Episode(public_id="ep_cccc0003", overview="荒诞与记忆概览",
               summary="C 摘要", source_text="深夜聊到荒诞主义,还有记忆系统的未来。",
               salience_tier=3, status="active", created_at="2026-06-28T09:00:00+00:00",
               activated_at="2026-06-28T09:00:00+00:00")


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def build_corpus() -> None:
    """写 3 条 episode 碎片 + index rebuild(FTS/向量随之建立)。碎片是正本。"""
    for ep in (EP_A, EP_B, EP_C):
        write_episode(CFG.episodes_dir, ep)
    rep = rebuild(CFG, FakeProvider(model="fake", dim=16))
    assert rep.episodes == 3, rep
    assert rep.vectors == 3, rep


def _row(con, public_id: str) -> dict:
    r = con.execute(
        "SELECT id, salience_tier, last_accessed_at, activated_at, created_at "
        "FROM episodes WHERE public_id=?", (public_id,)).fetchone()
    return dict(r)


# ============ S6-1:衰减模块(纯函数 + 时钟机制)============
def seg_s6_1() -> None:
    rc = CFG.recall

    # 半衰期数学
    a1 = decay.effective_activation(D14, 1, rc, NOW)         # tier1 半衰期 14 天,elapsed 14
    assert abs(a1 - 0.5) < 1e-6, a1
    a0 = decay.effective_activation(NOW.isoformat(), 1, rc, NOW)  # elapsed 0
    assert abs(a0 - 1.0) < 1e-9, a0
    a3 = decay.effective_activation(D14, 3, rc, NOW)         # tier3 半衰期 365 天,elapsed 14
    assert a3 > 0.9, a3
    ok(f"半衰期数学:tier1/14天={a1:.6f}≈0.5、elapsed0={a0:.6f}=1.0、tier3/14天={a3:.4f}>0.9")

    # NULL 回退链:last_accessed → activated → created
    f_act = decay.effective_activation(None, 1, rc, NOW, activated_at=D14, created_at=OLD)
    assert abs(f_act - 0.5) < 1e-6, f_act  # 回退到 activated(=D14)
    f_cre = decay.effective_activation(None, 1, rc, NOW, activated_at=None, created_at=D14)
    assert abs(f_cre - 0.5) < 1e-6, f_cre  # 再回退到 created(=D14)
    f_non = decay.effective_activation(None, 1, rc, NOW)
    assert abs(f_non - 1.0) < 1e-9, f_non  # 三者全空 → 视作刚活跃,不抛
    ok("NULL 回退链:last_accessed→activated→created 逐级回退,全空回落 1.0")

    # touch 后活跃度回 1.0(用 B,细节检索的"记忆系统"query 永不命中它,不受 S6-2 干扰)
    con = connect(CFG.db_path)
    try:
        b = _row(con, "ep_bbbb0002")
        before = decay.effective_activation(
            b["last_accessed_at"], b["salience_tier"], rc, NOW,
            activated_at=b["activated_at"], created_at=b["created_at"])
        assert before < 0.9, f"rebuild 后 last_accessed=activated(6/15),14+天前应明显衰减: {before}"
        decay.touch_episodes(con, [b["id"]], NOW)
        con.commit()
        b2 = _row(con, "ep_bbbb0002")
        after = decay.effective_activation(
            b2["last_accessed_at"], b2["salience_tier"], rc, NOW,
            activated_at=b2["activated_at"], created_at=b2["created_at"])
        assert abs(after - 1.0) < 1e-9, after
        assert b2["last_accessed_at"] == NOW.isoformat(), b2["last_accessed_at"]
    finally:
        con.close()
    ok(f"touch:刷新前活跃度={before:.4f}→刷新后=1.0(last_accessed 更新到 NOW)")


# ============ S6-2:细节检索(FTS grep + 开窗 + 时钟 + 红线)============
def seg_s6_2() -> None:
    import json

    from memory_system.cli import main as cli_main

    # 记录未命中条目(B)刷新前的时钟,用于"未命中不动"断言
    con = connect(CFG.db_path)
    try:
        b_before = _row(con, "ep_bbbb0002")["last_accessed_at"]
    finally:
        con.close()

    # (1) 精确词命中 + 窗口含该词
    res = recall_detail(CFG, "蓝莓松饼", now=NOW)
    assert res["mode"] == "detail" and res["query"] == "蓝莓松饼"
    assert [h["public_id"] for h in res["hits"]] == ["ep_bbbb0002"], res["hits"]
    assert "蓝莓" in res["hits"][0]["window"], res["hits"][0]["window"]
    ok("精确词命中:蓝莓松饼 → ep_bbbb0002,窗口含命中词")

    # (2) 未命中词 → 空且函数正常返回(退出码 0 在 CLI 层验证)
    miss = recall_detail(CFG, "根本不存在的怪词组合", now=NOW)
    assert miss["hits"] == [], miss
    rc_code = cli_main(["recall", "detail", "根本不存在的怪词组合"])
    assert rc_code == 0, rc_code
    ok("未命中:空 hits 且 CLI 退出码 0(不静默,提示换词)")

    # (3) --since 排除窗外条目:"记忆系统"本命中 A(6/1)+C(6/28),since=6/10 应只剩 C
    both = recall_detail(CFG, "记忆系统", now=NOW)
    assert {h["public_id"] for h in both["hits"]} == {"ep_aaaa0001", "ep_cccc0003"}, both["hits"]
    since_c = recall_detail(CFG, "记忆系统", since="2026-06-10", now=NOW)
    assert [h["public_id"] for h in since_c["hits"]] == ["ep_cccc0003"], since_c["hits"]
    ok("--since:记忆系统 双命中 → since=6/10 排除 6/1 的 A,只剩 C")

    # (4) 时钟:命中(A、C)被刷到 NOW;未命中(B)不动
    con = connect(CFG.db_path)
    try:
        a = _row(con, "ep_aaaa0001")
        c = _row(con, "ep_cccc0003")
        b = _row(con, "ep_bbbb0002")
        assert a["last_accessed_at"] == NOW.isoformat(), a["last_accessed_at"]
        assert c["last_accessed_at"] == NOW.isoformat(), c["last_accessed_at"]
        assert b["last_accessed_at"] == b_before, "未命中的 B 时钟不该被 detail 刷新"
    finally:
        con.close()
    ok("时钟刷新:命中 A/C 刷到 NOW,未命中 B 原封不动")

    # (5) --raw 返回整条 source_text(逐字,不开窗)
    raw = recall_detail(CFG, "蓝莓松饼", raw=True, now=NOW)
    assert raw["hits"][0]["window"] == EP_B.source_text, raw["hits"][0]["window"]
    ok("--raw:返回整条 source_text(逐字保真)")

    # (6) 红线:--json 输出不含 uuid / embedding / DB 整数 id
    blob = json.dumps(recall_detail(CFG, "记忆系统", now=NOW), ensure_ascii=False)
    for banned in ("uuid", "embedding", "\"id\""):
        assert banned not in blob, f"红线破:输出含 {banned!r}"
    hit0 = both["hits"][0]
    assert set(hit0.keys()) == {"public_id", "window", "created_at", "salience_tier"}, hit0.keys()
    ok("红线:--json 无 uuid / 无 embedding / 无 DB id,只对外露 public_id + 窗口 + 日期 + tier")


# ============ S6-3:情景检索(双路 RRF + 三槽 + 时钟规则)============
def seg_s6_3() -> None:
    import json
    from dataclasses import replace

    from memory_system.fragments import Node, write_node
    from memory_system.recall import recall_episode

    # 语料设计(fake embedding:只有**完全相同**的文本才向量距离 0,见施工书 §6.6):
    #   Q1 短语只出现在 D、E 的 source_text(FTS 路 = {D,E});
    #   D/F/G 的 overview 与 Q1 完全相同(向量距离 0,k=3 时向量路 = {D,F,G},E 被挤出)。
    #   ⇒ D 双路命中,E 是 FTS 单路、F/G 是向量单路。
    #   H/I 与 D 同 session(sess-ep),created_at 紧邻 D 前后(同源槽);两路都不命中。
    #   J 与 D 共享 node「曲率引擎」但不在任何槽内(联想槽,via_nodes);
    #   K 的 overview = Q2 且 Q2 不出现在任何 source_text(FTS 空手 → 向量单路照常)。
    Q1 = "星际航行与曲率引擎"
    Q2 = "泛银河系漫游手册第二版"

    def mk(pid: str, ov: str, src: str, created: str, *, tier: int = 1,
           sess: str | None = None, nodes: list[str] | None = None,
           highlights: list[dict] | None = None) -> Episode:
        return Episode(public_id=pid, overview=ov, summary=f"{pid} 摘要", source_text=src,
                       salience_tier=tier, status="active", created_at=created,
                       activated_at=created, source_session_id=sess,
                       nodes=nodes or [], highlights=highlights or [])

    eps = [
        mk("ep_dddd0004", Q1, f"我们聊了{Q1}的可行性,还有曲率泡的能量需求。",
           "2026-06-20T12:00:00+00:00", tier=2, sess="sess-ep", nodes=["曲率引擎"],
           highlights=[{"text": "曲率泡", "tag": "术语"}]),
        mk("ep_eeee0005", "园艺笔记与番茄支架的概览",
           f"顺带一提,{Q1}出现在了一本科幻小说里,页边还画着番茄支架的草图,以及很多别的琐事。",
           "2026-06-21T12:00:00+00:00"),
        mk("ep_ffff0006", Q1, "星舰推进的另一段随笔,没有那个短语出现。",
           "2026-06-19T12:00:00+00:00"),
        mk("ep_gggg0007", Q1, "又一段推进器讨论,同样不含目标短语。",
           "2026-06-18T12:00:00+00:00"),
        mk("ep_hhhh0008", "修水管概览", "修水管的一天,和检索毫无关系。",
           "2026-06-20T11:00:00+00:00", sess="sess-ep"),
        mk("ep_iiii0009", "买菜概览", "买菜清单与晚饭安排。",
           "2026-06-20T13:00:00+00:00", sess="sess-ep"),
        mk("ep_jjjj0010", "曲率引擎的哲学随想概览", "从推进器聊到存在主义,但没有那个查询词组。",
           "2026-06-10T12:00:00+00:00", sess="sess-other", nodes=["曲率引擎"]),
        mk("ep_kkkk0011", Q2, "这条的原文完全不含它自己的标题短语。", NOW.isoformat()),
    ]
    for ep in eps:
        write_episode(CFG.episodes_dir, ep)
    write_node(CFG.nodes_dir, Node(label="曲率引擎", type="concept",
                                   created_at="t0", updated_at="t0"))
    rep = rebuild(CFG, FakeProvider(model="fake", dim=16))  # 重置全部时钟(运行态,预期行为)
    assert rep.episodes == 11 and rep.vectors == 11 and not rep.stub_nodes, rep

    # candidate_multiplier 压到 1 ⇒ 每路各取 topk_final=3 条:让"单路/双路"可精确构造。
    cfg3 = replace(CFG, recall=replace(CFG.recall, candidate_multiplier=1))

    # 记录全部时钟基线(= rebuild 后的 activated_at)
    con = connect(CFG.db_path)
    try:
        before = {pid: _row(con, pid)["last_accessed_at"]
                  for pid in ["ep_dddd0004", "ep_eeee0005", "ep_ffff0006", "ep_gggg0007",
                              "ep_hhhh0008", "ep_iiii0009", "ep_jjjj0010", "ep_kkkk0011"]}
    finally:
        con.close()

    res = recall_episode(cfg3, Q1, now=NOW)
    slots = res["slots"]
    prim = slots["primary"]

    # (1) 双路命中的 D:RRF 分高于全部单路候选(E/F/G),稳居 top-1
    assert res["mode"] == "episode" and res["query"] == Q1
    assert len(prim) == cfg3.recall.topk_final == 3, [p["public_id"] for p in prim]
    assert prim[0]["public_id"] == "ep_dddd0004", [p["public_id"] for p in prim]
    assert prim[0]["score"] > prim[1]["score"] and prim[0]["score"] > prim[2]["score"], prim
    assert {p["public_id"] for p in prim} <= {"ep_dddd0004", "ep_eeee0005",
                                              "ep_ffff0006", "ep_gggg0007"}, prim
    ok("双路 RRF:D(向量+FTS)分数压过单路的 E/F/G,主槽条数 = topk_final")

    # (2) 同源槽:确实来自 top-1 的 session(sess-ep)且时间紧邻(H 在前、I 在后)
    same = slots["same_source"]
    assert [h["public_id"] for h in same] == ["ep_hhhh0008", "ep_iiii0009"], same
    assert all(set(h.keys()) == {"public_id", "summary", "highlights", "created_at"}
               for h in same), same
    ok("同源槽:sess-ep 内紧邻 D 前后的 H/I,只带 summary 级字段(无 source_text)")

    # (3) 联想槽:经膜(曲率引擎)跳到 J,via_nodes 正确,与前两槽不重复
    assoc = slots["associative"]
    assert res["frame_nodes"] == ["曲率引擎"], res["frame_nodes"]
    assert [h["public_id"] for h in assoc] == ["ep_jjjj0010"], assoc
    assert assoc[0]["via_nodes"] == ["曲率引擎"], assoc
    assert all(set(h.keys()) == {"public_id", "summary", "highlights", "via_nodes"}
               for h in assoc), assoc
    ids_all = [p["public_id"] for p in prim] + [h["public_id"] for h in same] + \
              [h["public_id"] for h in assoc]
    assert len(ids_all) == len(set(ids_all)), f"三槽不许重复: {ids_all}"
    ok("联想槽:D 经膜「曲率引擎」跳到 J,via_nodes 正确,三槽无重复")

    # (4) 时钟:只刷 top-1(D)+ 同源(H/I);主槽非 top-1、联想槽、未命中全不动
    con = connect(CFG.db_path)
    try:
        after = {pid: _row(con, pid)["last_accessed_at"] for pid in before}
    finally:
        con.close()
    for pid in ("ep_dddd0004", "ep_hhhh0008", "ep_iiii0009"):
        assert after[pid] == NOW.isoformat(), (pid, after[pid])
    for pid in ("ep_eeee0005", "ep_ffff0006", "ep_gggg0007", "ep_jjjj0010", "ep_kkkk0011"):
        assert after[pid] == before[pid], f"{pid} 的时钟不该被刷(主槽非 top-1/联想/未命中)"
    ok("时钟:top-1 D + 同源 H/I 刷到 NOW;主槽二三名与联想槽 J 未刷")

    # (5) FTS 空手(Q2 短语不在任何 source_text):不崩,向量单路照常出结果
    res2 = recall_episode(cfg3, Q2, now=NOW)
    prim2 = res2["slots"]["primary"]
    assert len(prim2) == 3 and prim2[0]["public_id"] == "ep_kkkk0011", \
        [p["public_id"] for p in prim2]
    ok("FTS 空手:不崩,向量单路照常,overview=Q2 的 K 居 top-1")

    # (6) 红线 + 契约形状:主槽带 source_text,--json 无 uuid / embedding / DB id
    blob = json.dumps(res, ensure_ascii=False)
    for banned in ("uuid", "embedding", "\"id\""):
        assert banned not in blob, f"红线破:输出含 {banned!r}"
    assert set(prim[0].keys()) == {"public_id", "overview", "summary", "highlights",
                                   "source_text", "created_at", "salience_tier", "score"}, \
        prim[0].keys()
    assert prim[0]["highlights"] == [{"text": "曲率泡", "tag": "术语"}], prim[0]["highlights"]
    ok("红线:episode 输出无 uuid / 无 embedding / 无 DB id;主槽字段形状照 §5 契约")


# ============ S6-4:概念检索(入口三路径 + 概念层无原文 + node 时钟)============
def seg_s6_4() -> None:
    import json

    from memory_system.cli import main as cli_main
    from memory_system.fragments import Node, write_node
    from memory_system.recall import recall_concept
    from memory_system.recall.concept import NodeMissError

    # 给「曲率引擎」补别名(碎片是正本,别名从碎片来)后重建;rebuild 再次重置时钟,预期行为。
    write_node(CFG.nodes_dir, Node(label="曲率引擎", type="concept",
                                   created_at="t0", updated_at="t0", aliases=["warp航法"]))
    rep = rebuild(CFG, FakeProvider(model="fake", dim=16))
    assert rep.episodes == 11 and rep.aliases == 1, rep

    # 时钟基线:rebuild 后 node 时钟为 NULL(运行态),episode 时钟 = activated_at
    con = connect(CFG.db_path)
    try:
        d_before = _row(con, "ep_dddd0004")["last_accessed_at"]
        j_before = _row(con, "ep_jjjj0010")["last_accessed_at"]
        n_before = con.execute(
            "SELECT last_accessed_at FROM nodes WHERE label='曲率引擎'").fetchone()[0]
    finally:
        con.close()
    assert n_before is None, n_before

    # (1) label 直查:全量取(D+J 无 top-k)、无桥接、默认排序 tier 降序(D tier2 前于 J tier1)
    res = recall_concept(CFG, "曲率引擎", now=NOW)
    assert res["mode"] == "concept" and res["node"] == "曲率引擎"
    assert res["alias_bridge"] is None, res["alias_bridge"]
    assert [e["public_id"] for e in res["episodes"]] == ["ep_dddd0004", "ep_jjjj0010"], \
        res["episodes"]
    ok("label 直查:全量取 D+J、alias_bridge=null、默认排序 tier 降序")

    # (2) alias 查:命中同一 node,带桥接行
    res_a = recall_concept(CFG, "warp航法", now=NOW)
    assert res_a["node"] == "曲率引擎"
    assert res_a["alias_bridge"] == "『warp航法』= 概念 曲率引擎", res_a["alias_bridge"]
    assert [e["public_id"] for e in res_a["episodes"]] == \
        [e["public_id"] for e in res["episodes"]]
    ok("alias 查:warp航法 → 同一 node,带 alias_bridge 桥接行")

    # (3) miss:抛 NodeMissError 且子串建议含相近 label;CLI 友好报错退出码 1
    try:
        recall_concept(CFG, "曲率", now=NOW)
        raise AssertionError("miss 应抛 NodeMissError")
    except NodeMissError as e:
        assert "曲率引擎" in e.suggestions, e.suggestions
    rc_code = cli_main(["recall", "concept", "完全不存在的概念"])
    assert rc_code == 1, rc_code
    ok("miss:NodeMissError 带子串建议(曲率→曲率引擎),CLI 退出码 1 不静默")

    # (4) 概念层留在概念层:无 source_text;字段形状照 §5 契约;红线同前
    blob = json.dumps(res, ensure_ascii=False)
    assert "source_text" not in blob, "概念检索绝不返回 source_text"
    for banned in ("uuid", "embedding", "\"id\""):
        assert banned not in blob, f"红线破:输出含 {banned!r}"
    for e in res["episodes"]:
        assert set(e.keys()) == {"public_id", "summary", "highlights",
                                 "salience_tier", "activation", "created_at"}, e.keys()
    assert res["episodes"][0]["highlights"] == [{"text": "曲率泡", "tag": "术语"}]
    assert 0.0 < res["episodes"][0]["activation"] <= 1.0
    ok("概念层:无 source_text、字段照 §5 契约(含 activation)、红线全过")

    # (5) 时钟:node 自己刷到 NOW;下属 episode(D/J)未动
    con = connect(CFG.db_path)
    try:
        n_after = con.execute(
            "SELECT last_accessed_at FROM nodes WHERE label='曲率引擎'").fetchone()[0]
        d_after = _row(con, "ep_dddd0004")["last_accessed_at"]
        j_after = _row(con, "ep_jjjj0010")["last_accessed_at"]
    finally:
        con.close()
    assert n_after == NOW.isoformat(), n_after
    assert d_after == d_before and j_after == j_before, "概念检索不许刷下属 episode 时钟"
    ok("时钟:node 刷到 NOW,下属 episode D/J 原封不动")

    # (6) --context 排序:context 与 J 的 overview 全同(fake 向量距离 0)→ J 越过高 tier 的 D
    res_c = recall_concept(CFG, "曲率引擎", context="曲率引擎的哲学随想概览", now=NOW)
    assert [e["public_id"] for e in res_c["episodes"]] == ["ep_jjjj0010", "ep_dddd0004"], \
        res_c["episodes"]
    ok("--context:按语境相似度排,J(与 context 同文)越过高 tier 的 D")


# ============ S6-5:重构 agent 接入(fake chat:测管线不是文采)============
def seg_s6_5() -> None:
    import io
    from contextlib import redirect_stdout

    from memory_system.agent.base import ChatError
    from memory_system.agent.fake import FakeChatProvider
    from memory_system.cli import main as cli_main
    from memory_system.recall import recall_episode, reconstruct

    Q1 = "星际航行与曲率引擎"

    # (1) 引擎:三部分输入铁律(system=prompt 文件全文 / user=结构化 JSON+当轮 query),
    #     输出 = provider 返回值,调用计数 +1
    seen: dict = {}

    def _capture(system: str, user: str, model: str) -> str:
        seen.update(system=system, user=user, model=model)
        return "这是一段假回忆。"

    fake = FakeChatProvider(behaviors=[_capture])
    structured = recall_episode(CFG, Q1, touch=False)
    text = reconstruct.run(CFG, "episode", structured, Q1, provider=fake)
    assert text == "这是一段假回忆。" and fake.calls == 1
    want_sys = (reconstruct._PROMPT_DIR / "recall_episode_system.txt").read_text(encoding="utf-8")
    assert seen["system"] == want_sys, "system 必须是 prompt 文件全文(不硬编码)"
    assert "## 结构化检索结果" in seen["user"] and "## 用户当轮 query" in seen["user"]
    assert Q1 in seen["user"] and "ep_dddd0004" in seen["user"]
    assert seen["model"] == CFG.agent.recall_model
    ok("reconstruct.run:三部分输入(prompt 文件/结构化 JSON/当轮 query),输出=provider 返回值")

    # (2)–(4) CLI 管线:替换 reconstruct 的 provider 工厂为受控 fake
    box = {"p": FakeChatProvider(behaviors=["回忆:曲率引擎的那一晚。"])}
    orig_factory = reconstruct.get_chat_provider
    reconstruct.get_chat_provider = lambda agent_cfg: box["p"]
    try:
        # (2) 默认路径走重构:stdout = fake 返回值,chat 被调用一次
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc0 = cli_main(["recall", "concept", "曲率引擎"])
        assert rc0 == 0 and box["p"].calls == 1, (rc0, box["p"].calls)
        assert buf.getvalue().strip() == "回忆:曲率引擎的那一晚。", buf.getvalue()
        ok("默认路径:concept 走重构,输出 = fake 返回值,chat 调用 1 次")

        # (3) --raw / --json 不调 chat;detail 分支完全不碰 reconstruct
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert cli_main(["recall", "concept", "曲率引擎", "--raw"]) == 0
            assert cli_main(["recall", "episode", Q1, "--raw"]) == 0
            assert cli_main(["recall", "episode", Q1, "--json"]) == 0
            assert cli_main(["recall", "detail", "蓝莓松饼"]) == 0
        assert box["p"].calls == 1, "--raw/--json/detail 绝不调 chat provider"
        assert "ep_" in buf.getvalue(), "结构化输出仍应给出 public_id"
        ok("--raw/--json/detail:不调 chat provider(计数不增),结构化输出照常")

        # (4) ChatError 注入:降级为结构化输出、退出码非零、不吞结果
        box["p"] = FakeChatProvider(behaviors=[ChatError("模拟后端失败")])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc3 = cli_main(["recall", "episode", Q1])
        out = buf.getvalue()
        assert rc3 == 3, rc3
        assert "重构失败" in out and "ep_dddd0004" in out, out
        ok("ChatError 降级:回落 --raw 结构化输出(不吞结果),退出码 3")
    finally:
        reconstruct.get_chat_provider = orig_factory

    # (5) 日志:重构前候选集(public_id + 槽位)落文件,召回可重放
    log_text = (CFG.logs_dir / "memory_system.log").read_text(encoding="utf-8")
    assert "重构候选集" in log_text, "候选集必须写日志"
    assert '"primary"' in log_text and "ep_dddd0004" in log_text, "episode 槽位+public_id 应在日志里"
    assert '"node": "曲率引擎"' in log_text, "concept 候选集应在日志里"
    ok("日志:候选集(槽位 + public_id)已落 logs/memory_system.log,召回可重放")


# ============ S6-6:开场注入(选材填槽 + 只读窥视 + dirty 写入侧接线)============
def seg_s6_6() -> None:
    import io
    import json as _json
    from contextlib import redirect_stdout

    from memory_system import archive, staging_store
    from memory_system.agent.fake import FakeChatProvider, make_extraction
    from memory_system.chunk import manual_segments
    from memory_system.cli import main as cli_main
    from memory_system.embedding import get_provider
    from memory_system.extract import extract_segments
    from memory_system.preprocess import CleanedTranscript, Turn
    from memory_system.recall import opening, reconstruct

    cache = opening.cache_path(CFG)
    dirty = CFG.opening_cache_dir / ".dirty"
    # 自取干净基线:前面各段没走过写入侧(直接 write_episode + rebuild),防御性清一遍。
    opening.clear_dirty(CFG)
    if cache.exists():
        cache.unlink()

    # 当前库里 created_at 最新的 active(seg_s6_3 造的 K,created_at=NOW=2026-07-02,全库最大)。
    con = connect(CFG.db_path)
    try:
        newest = con.execute(
            "SELECT public_id FROM episodes WHERE status='active' "
            "ORDER BY created_at DESC, public_id LIMIT 1").fetchone()[0]
        before_clocks = {pid: lac for (pid, lac) in con.execute(
            "SELECT public_id, last_accessed_at FROM episodes")}
    finally:
        con.close()
    assert newest == "ep_kkkk0011", newest

    # (1) 选材:槽 A=最新条;槽 B tier>=2、去重槽 A;硬顶 opening_max_items;spark(槽 C)不建
    material = opening.select_opening(CFG, now=NOW)
    latest, ballast = material["slots"]["latest"], material["slots"]["ballast"]
    assert material["mode"] == "opening"
    assert material["token_budget"] == CFG.recall.opening_token_budget
    assert len(latest) == 1 and latest[0]["public_id"] == newest, latest
    assert all(b["salience_tier"] >= 2 for b in ballast), ballast
    assert newest not in {b["public_id"] for b in ballast}
    ids_all = [e["public_id"] for e in latest] + [e["public_id"] for e in ballast]
    assert len(ids_all) <= CFG.recall.opening_max_items, ids_all
    assert len(ids_all) == len(set(ids_all)), ids_all
    ok("选材:槽 A=最新条 K、槽 B 全 tier>=2 且不含 A、总条数≤opening_max_items 且不重复")

    # (2) 红线 + 概念纪律:选材中间态不带 uuid / embedding / DB id / source_text
    blob = _json.dumps(material, ensure_ascii=False)
    for banned in ("uuid", "embedding", "\"id\"", "source_text"):
        assert banned not in blob, f"红线破:开场选材含 {banned!r}"
    ok("红线:开场选材无 uuid / embedding / DB id / source_text(开场只用 overview/summary/highlights)")

    # (3) rebuild(engine + 受控 fake chat):cache 非空、三部分输入、选材进 user、成功删 dirty
    seen: dict = {}

    def _cap(system: str, user: str, model: str) -> str:
        seen.update(system=system, user=user, model=model)
        return "开场:上次我们聊到曲率引擎,而记忆系统一直是底色。"

    fake = FakeChatProvider(behaviors=[_cap])
    opening.mark_dirty(CFG)
    assert dirty.exists()
    text = opening.rebuild_opening(CFG, provider=fake, now=NOW)
    assert fake.calls == 1 and text and text.startswith("开场:"), text
    assert cache.exists() and cache.read_text(encoding="utf-8").strip(), "cache 应非空"
    assert not dirty.exists(), "rebuild 成功应删 .dirty"
    want_sys = (reconstruct._PROMPT_DIR / "opening_system.txt").read_text(encoding="utf-8")
    assert seen["system"] == want_sys, "system 必须是 opening prompt 文件全文(不硬编码)"
    assert "## 结构化检索结果" in seen["user"] and newest in seen["user"], seen["user"]
    assert seen["model"] == CFG.agent.recall_model
    ok("rebuild:受控 fake 重构,cache 非空、三部分输入(prompt 文件/选材 JSON/占位 query)、删 dirty")

    # (4) 只读窥视:选材 + rebuild 前后,所有 episode 的 last_accessed_at 零变化(裁定:开场全不刷)
    con = connect(CFG.db_path)
    try:
        after_clocks = {pid: lac for (pid, lac) in con.execute(
            "SELECT public_id, last_accessed_at FROM episodes")}
    finally:
        con.close()
    assert after_clocks == before_clocks, "开场注入是只读窥视,绝不许刷任何 episode 时钟"
    ok("只读窥视:选材 + rebuild 前后所有 episode 时钟零变化")

    # (5) dirty 门:无 dirty 时 rebuild 跳过(返回 None、不调 chat);--force 无视 dirty 强跑
    assert not dirty.exists()
    skip_p = FakeChatProvider(behaviors=["不该被调用"])
    assert opening.rebuild_opening(CFG, provider=skip_p, now=NOW) is None
    assert skip_p.calls == 0, "无 dirty 且未 force,绝不调 chat provider"
    force_p = FakeChatProvider(behaviors=["强制重建的开场独白。"])
    ftext = opening.rebuild_opening(CFG, force=True, provider=force_p, now=NOW)
    assert force_p.calls == 1 and ftext == "强制重建的开场独白。"
    assert cache.read_text(encoding="utf-8").strip() == "强制重建的开场独白。"
    ok("dirty 门:无 dirty→rebuild 跳过(不调 chat);--force 无视 dirty 强跑并覆盖 cache")

    # (6) CLI 面:opening show 缺失提示 / rebuild --force / show cat(monkeypatch chat 工厂,同 seg_s6_5)
    box = {"p": FakeChatProvider(behaviors=["CLI 强制重建开场。"])}
    orig = reconstruct.get_chat_provider
    reconstruct.get_chat_provider = lambda agent_cfg: box["p"]
    try:
        cache.unlink()  # 先删,验证 show 在缺失时提示先 rebuild
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_miss = cli_main(["opening", "show"])
        assert rc_miss == 1 and "rebuild" in buf.getvalue(), buf.getvalue()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_rb = cli_main(["opening", "rebuild", "--force"])
        assert rc_rb == 0 and box["p"].calls == 1, (rc_rb, box["p"].calls)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_show = cli_main(["opening", "show"])
        assert rc_show == 0 and "CLI 强制重建开场。" in buf.getvalue(), buf.getvalue()
    finally:
        reconstruct.get_chat_provider = orig
    ok("CLI:opening show 缺失提示 rebuild(退出码 1)、rebuild --force 走通、show cat 出内容")

    # (7) 写入侧接线:confirm 一条新 active episode → .dirty 出现(archive.confirm_episode 一行接线)
    opening.clear_dirty(CFG)
    assert not dirty.exists()
    cto = CleanedTranscript(session_id="sess-opening", path="/fake/sess-opening.jsonl")
    for i in range(1, 5):
        cto.turns.append(Turn(idx=i, human_text=f"人类第{i}句", assistant_text=f"Claude第{i}句",
                              uuids=[f"o{i}"], human_uuid=f"o{i}",
                              timestamp=f"2026-06-30T10:{i:02d}:00Z"))
    osegs = manual_segments(cto, [(1, 4)])
    osegs[0]["seg_id"] = "s1"
    obatch = extract_segments(
        cto, osegs,
        FakeChatProvider(behaviors=[make_extraction(
            overview="开场写入侧接线测试段 overview。", summary="接线测试段。",
            nodes=[], salience_tier=1)]),
        [], model="opus", timeout=10, max_retries=0)
    assert len(obatch.staged) == 1
    oseg, ores, osrc = obatch.staged[0]
    staging_store.upsert_episode(CFG.staging_episodes_dir, cto.session_id, cto.path,
                                 oseg, ores, osrc, created_at="2026-06-30T10:01:00Z")
    new_pid = archive.confirm_episode(CFG, cto.session_id, "e1", get_provider(CFG.embedding))
    assert new_pid.startswith("ep_")
    assert dirty.exists(), "confirm 新 episode 后开场缓存应被标记 .dirty(写入侧接线)"
    ok("写入侧接线:confirm 新 episode → opening .dirty 出现")


def main() -> None:
    build_corpus()
    seg_s6_1()
    seg_s6_2()
    seg_s6_3()
    seg_s6_4()
    seg_s6_5()
    seg_s6_6()
    print("S6 检索层 ALL PASS ✅")


if __name__ == "__main__":
    main()
