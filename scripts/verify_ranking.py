"""S6.5 轻量重排(ranking.py)headless 验收(纯函数,离线,不碰 DB / embedding)。

覆盖 spec §2.5 六组断言:
- seg_1 锚点抽取:混合 query 抽出 node/alias、英文(casefold)、数字、整段、bigram 各类,
        权重/idf 正确;停用词降权;同 text 去重取最高权;>6 字 run 不出整段。
- seg_2 旗舰场景:泛化条目 vector_rank=1 + 高活跃,具体条目 rank=2 覆盖高权锚点 →
        重排后具体条目在前(gap 惩罚泛化条目)。
- seg_3 泛 query:锚点全是停用词(少且弱)→ gap 惩罚小(<0.1)且各候选相等,
        RRF 序保持(不误杀综合条目)。
- seg_4 无锚点退化:anchors 空、coverage/gap 全 0,排序 = w_rrf·rrf_norm + w_act·act 序。
- seg_5 确定性:同输入两次调用逐字段相等;relevance 相同(人工构造)时按
        created_at/public_id 定序(与主槽 tie-break 一致,可重放)。
- seg_6 features 键齐全,数值在文档化区间(rrf_norm/coverage/gap/activation ∈ [0,1],
        relevance ∈ [−w_gap, w_rrf+w_anchor+w_activation]),scores 与 features 一致。

跑法:.venv/bin/python scripts/verify_ranking.py
"""

from __future__ import annotations

import math

from memory_system.config import RecallConfig
from memory_system.recall.ranking import (
    EpisodeCandidate,
    extract_anchors,
    rank_episode_candidates,
)

RC = RecallConfig()


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def cand(eid: int, pid: str, *, overview: str = "", summary: str = "",
         highlights: list[str] | None = None, source_text: str = "",
         node_terms: list[str] | None = None,
         created_at: str = "2026-06-01T00:00:00+00:00",
         vec: int | None = None, fts: int | None = None,
         act: float = 0.0) -> EpisodeCandidate:
    return EpisodeCandidate(
        eid=eid, public_id=pid, overview=overview, summary=summary,
        highlights=highlights or [], source_text=source_text,
        node_terms=node_terms or [], created_at=created_at,
        vector_rank=vec, fts_rank=fts, activation=act)


def seg_1_anchors() -> None:
    print("[seg_1] 锚点抽取:混合 query 各类齐出、停用词降权、去重取最高权")
    q = "处理 IEEE 论文 PDF 时分块老把公式拆开,recall@10 掉到 0.85"
    anchors = extract_anchors(q, ["文档分块", "分块", "PDF"])
    amap = {a.text: a for a in anchors}
    assert len(amap) == len(anchors), "去重后 text 应唯一"
    ok(f"共 {len(anchors)} 个锚点,text 唯一")

    # node/alias:区分大小写按原样;词表项不在 query 里不出锚点
    assert amap["分块"].weight == 1.4 and amap["分块"].idf_proxy == 1.00, amap["分块"]
    assert amap["PDF"].weight == 1.4, amap["PDF"]
    assert "文档分块" not in amap, "词表项非 query 子串不应出锚点"
    ok("node/alias:分块/PDF 命中 1.4,「文档分块」不在 query 中不出")
    # 「分块」同时是长 run 的 bigram(1.0),去重取最高权 → 1.4
    ok("去重:「分块」bigram 与 node 同 text,保留最高权 1.4")

    # 英文/代码 token:统一 casefold
    for t in ("ieee", "pdf", "recall@10"):
        assert amap[t].weight == 1.3 and amap[t].idf_proxy == 0.90, amap[t]
    assert "IEEE" not in amap, "英文锚点应 casefold 存储"
    ok("英文 token:ieee/pdf/recall@10 = 1.3(casefold;pdf 与 node『PDF』并存)")

    # 数字/指标:长度≥2
    assert amap["0.85"].weight == 1.2 and amap["0.85"].idf_proxy == 0.85, amap["0.85"]
    ok("数字:0.85 = 1.2/0.85")

    # 中文整段(2~6 字)与 bigram;>6 字 run 只出 bigram
    assert amap["论文"].weight == 1.1 and amap["论文"].idf_proxy == 0.70, amap["论文"]
    assert amap["掉到"].weight == 1.1, amap["掉到"]
    assert amap["公式"].weight == 1.0 and amap["公式"].idf_proxy == 0.55, amap["公式"]
    assert amap["拆开"].weight == 1.0, amap["拆开"]
    assert "时分块老把公式拆开" not in amap, ">6 字 run 不应出整段锚点"
    ok("中文:论文/掉到 整段 1.1;公式/拆开 bigram 1.0;9 字 run 无整段")

    # 停用词降权(整段与 bigram 同 text『处理』,降权后去重仍 0.3)
    assert amap["处理"].weight == 0.3 and amap["处理"].idf_proxy == 0.15, amap["处理"]
    ok("停用词:处理 → 0.3/0.15")

    # 无锚点 query → 空列表
    assert extract_anchors("水", ["分块"]) == []
    assert extract_anchors("", []) == []
    ok("无锚点 query(单字/空)→ 空列表")


def seg_2_flagship() -> None:
    print("[seg_2] 旗舰场景:具体条目 vector_rank=2 覆盖高权锚点 → 反超 rank=1 泛化条目")
    q = "IEEE 论文 PDF 分块"
    # 泛化条目:向量第 1 + 满活跃,但一个锚点都盖不住
    generic = cand(1, "ep_generic", overview="日常工作流总结",
                   summary="泛化的近期活动摘要", source_text="最近做了很多杂事。",
                   vec=1, act=1.0)
    # 具体条目:向量第 2、低活跃,node_terms/overview 盖满全部锚点
    specific = cand(2, "ep_specific", overview="讨论 IEEE 论文的 PDF 分块策略",
                    summary="公式被拆开的问题", node_terms=["分块"],
                    vec=2, act=0.2)
    r = rank_episode_candidates(q, [generic, specific], rc=RC,
                                node_terms=["分块", "向量检索"])
    assert r.ordered == [2, 1], r.ordered
    ok("重排后具体条目(eid=2)在前")
    assert r.features[2]["anchor_coverage"] == 1.0, r.features[2]
    assert r.features[1]["anchor_coverage"] == 0.0, r.features[1]
    ok("coverage:具体=1.0,泛化=0.0")
    # 本 query specificity 封顶 1.0 → 泛化条目 gap = 1.0,吃满惩罚
    assert math.isclose(r.features[1]["gap"], 1.0), r.features[1]
    assert r.features[2]["gap"] == 0.0, r.features[2]
    ok("gap:泛化=1.0(吃满 w_rank_gap 惩罚),具体=0.0")
    # 旧乘子思路下泛化条目会赢(rrf 高 + 活跃 1.0),这里被锚点特征翻盘
    assert r.scores[2] > r.scores[1] + 0.3, (r.scores, "翻盘幅度应显著")
    ok(f"relevance:具体 {r.scores[2]:.4f} ≫ 泛化 {r.scores[1]:.4f}")


def seg_3_generic_query() -> None:
    print("[seg_3] 泛 query(锚点少且弱):gap 惩罚小且同值,RRF 序保持")
    q = "那个,这个,怎么"  # 三段全是停用词 → 锚点全 0.3/0.15
    anchors = extract_anchors(q, [])
    assert anchors and all(a.weight == 0.3 for a in anchors), anchors
    ok(f"{len(anchors)} 个锚点全为停用词降权(0.3)")
    pool = [
        cand(31, "ep_a", overview="综合条目甲", vec=1, act=0.5),
        cand(32, "ep_b", overview="综合条目乙", vec=2, act=0.5),
        cand(33, "ep_c", overview="综合条目丙", vec=3, act=0.5),
    ]
    r = rank_episode_candidates(q, pool, rc=RC, node_terms=[])
    assert r.ordered == [31, 32, 33], r.ordered
    ok("三候选均不覆盖锚点 → RRF 序保持,综合条目未被误杀")
    gaps = [r.features[e]["gap"] for e in (31, 32, 33)]
    assert gaps[0] == gaps[1] == gaps[2], gaps
    assert RC.w_rank_gap * gaps[0] < 0.1, (gaps[0], "泛 query 惩罚应接近 0")
    ok(f"gap 同值且惩罚小:w_gap·gap = {RC.w_rank_gap * gaps[0]:.4f} < 0.1")


def seg_4_no_anchor_degrade() -> None:
    print("[seg_4] 无锚点 query 退化:排序 = RRF + activation 序")
    q = "水"  # 单 CJK 字:无任何锚点
    pool = [
        cand(11, "ep_low", vec=1, act=0.0),   # rrf 高、活跃 0
        cand(12, "ep_high", vec=2, act=1.0),  # rrf 略低、活跃 1 → 弱加项反超
    ]
    r = rank_episode_candidates(q, pool, rc=RC, node_terms=["分块"])
    assert r.anchors == [], r.anchors
    for e in (11, 12):
        assert r.features[e]["anchor_coverage"] == 0.0
        assert r.features[e]["gap"] == 0.0
    ok("anchors 空,coverage/gap 全 0")
    # 期望分 = w_rrf·rrf_norm + w_act·act(逐候选核对公式)
    for c in pool:
        rrf_n = (1.0 / (RC.rrf_k + c.vector_rank)) / (2.0 / (RC.rrf_k + 1))
        want = RC.w_rank_rrf * rrf_n + RC.w_rank_activation * c.activation
        assert math.isclose(r.scores[c.eid], want), (c.eid, r.scores[c.eid], want)
    assert r.ordered == [12, 11], r.ordered
    ok("relevance 逐候选等于 w_rrf·rrf_norm + w_act·act;活跃度弱加项决出顺序")


def seg_5_determinism() -> None:
    print("[seg_5] 确定性与 tie-break")
    q = "IEEE 论文 PDF 分块"
    pool = [
        cand(1, "ep_generic", overview="日常工作流总结", vec=1, act=1.0),
        cand(2, "ep_specific", overview="讨论 IEEE 论文的 PDF 分块策略",
             node_terms=["分块"], vec=2, act=0.2),
    ]
    r1 = rank_episode_candidates(q, pool, rc=RC, node_terms={"分块", "向量检索"})
    r2 = rank_episode_candidates(q, pool, rc=RC, node_terms={"向量检索", "分块"})
    assert r1.ordered == r2.ordered
    assert r1.scores == r2.scores
    assert r1.features == r2.features
    assert r1.anchors == r2.anchors
    ok("同输入(词表甚至传 set)两次调用:ordered/scores/features/anchors 逐字段相等")

    # 人工构造同分:无锚点 + 双路全 None + 同活跃 → relevance 全等,
    # 按 (created_at, public_id) 定序(与主槽 tie-break 一致)。
    ties = [
        cand(21, "ep_zzzz", created_at="2026-01-01T00:00:00+00:00"),
        cand(22, "ep_aaaa", created_at="2026-02-01T00:00:00+00:00"),
        cand(23, "ep_mmmm", created_at="2026-01-01T00:00:00+00:00"),
    ]
    rt = rank_episode_candidates("水", ties, rc=RC, node_terms=[])
    assert len({rt.scores[e] for e in (21, 22, 23)}) == 1, rt.scores
    assert rt.ordered == [23, 21, 22], rt.ordered
    ok("同分:先 created_at 升序,同日再 public_id 升序(23→21→22)")


def seg_6_features_contract() -> None:
    print("[seg_6] features 键齐全、数值区间")
    q = "处理 IEEE 论文 PDF 时分块老把公式拆开,recall@10 掉到 0.85"
    pool = [
        cand(41, "ep_full", overview="IEEE 论文 PDF 分块,recall@10 掉到 0.85",
             highlights=["公式拆开"], node_terms=["分块"], vec=1, fts=1, act=1.0),
        cand(42, "ep_half", summary="聊过论文的公式问题",
             source_text="当时说 recall@10 掉了。", vec=2, act=0.5),
        cand(43, "ep_none", overview="完全无关的一天", vec=None, fts=3, act=0.0),
    ]
    r = rank_episode_candidates(q, pool, rc=RC, node_terms=["分块"])
    keys = {"rrf_norm", "anchor_coverage", "gap", "activation", "relevance"}
    lo = -RC.w_rank_gap
    hi = RC.w_rank_rrf + RC.w_rank_anchor + RC.w_rank_activation
    for c in pool:
        f = r.features[c.eid]
        assert set(f) == keys, (c.eid, set(f))
        assert 0.0 <= f["rrf_norm"] <= 1.0, f
        assert 0.0 <= f["anchor_coverage"] <= 1.0, f
        assert 0.0 <= f["gap"] <= 1.0, f
        assert 0.0 <= f["activation"] <= 1.0, f
        assert lo <= f["relevance"] <= hi, (f, lo, hi)
        assert r.scores[c.eid] == f["relevance"], c.eid
    assert set(r.ordered) == {41, 42, 43}
    ok(f"三候选键齐全;各特征 ∈ [0,1],relevance ∈ [{lo:.2f}, {hi:.2f}];"
       f"scores 与 features 一致;ordered 覆盖全池")
    # 双路第一名 rrf_norm 恰为 1(归一定义)
    assert math.isclose(r.features[41]["rrf_norm"], 1.0), r.features[41]
    ok("双路第一名 rrf_norm = 1.0(归一化定义成立)")


def main() -> None:
    seg_1_anchors()
    seg_2_flagship()
    seg_3_generic_query()
    seg_4_no_anchor_degrade()
    seg_5_determinism()
    seg_6_features_contract()
    print("verify_ranking: 全部通过")


if __name__ == "__main__":
    main()
