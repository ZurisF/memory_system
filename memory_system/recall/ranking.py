"""轻量召回重排(S6.5):确定性锚点抽取 + anchor_coverage + specificity_gap。

裁定:召回评测已钉死病灶——episode miss 的期望条目 96% 已在原始候选池内(排 4~12 名),
是排序问题不是召回问题。候选池已含 96% 正确答案,本模块只负责最后一米的排序;
锚点=确定性规则(正则 + node 词表 + 停用词表),无 LLM。
纯函数、零依赖、不碰 DB、不 import embedding/agent;调用方(episode.py)备好
候选特征后整池送入。MMR / generic_penalty / df 表 / anchor FTS 均不做(v1 冰箱)。

线性分(权重在 RecallConfig 的 w_rank_*;锚点/字段权重是本模块定稿常量,调参不动这里):
    relevance = w_rrf·rrf_norm + w_anchor·anchor_coverage
              + w_activation·activation − w_gap·gap
    gap = query_specificity · (1 − anchor_coverage)
activation 从旧乘子(w_activation)降为弱加项(tie-break 级);gap 只在 query 足够
具体而候选盖不住锚点时惩罚,泛 query 下 specificity 低、惩罚自然趋零(不误杀综合条目)。
无锚点 query:coverage=0、gap=0,退化为 RRF+activation 序。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from memory_system.config import RecallConfig

# ---- 锚点类型权重 / idf 近似(spec §2.3 定稿) ----
_W_NODE, _IDF_NODE = 1.4, 1.00        # node/alias 命中(最强证据)
_W_EN, _IDF_EN = 1.3, 0.90            # 英文/代码 token
_W_NUM, _IDF_NUM = 1.2, 0.85          # 数字/指标
_W_RUN, _IDF_RUN = 1.1, 0.70          # 中文整段 run(2~6 字)
_W_BIGRAM, _IDF_BIGRAM = 1.0, 0.55    # 中文 bigram
_W_STOP, _IDF_STOP = 0.3, 0.15        # 停用词降权(只作用于整段/bigram 两类)

_EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.+#/@-]{1,}")
_NUM_TOKEN = re.compile(r"\d+(?:[.:∶]\d+)*%?")

# 中文口水词:整段/bigram 命中即降权为 0.3/0.15(可后续增补)。
_STOPWORDS = frozenset("""
系统 项目 方案 问题 优化 处理 讨论 之前 那个 这个 时候 怎么 什么 如何 我们 你们
今天 昨天 记得 提到 说过 聊过 关于 一下 可以 应该 需要 现在 后来 当时 觉得 还是
是不是 有没有 怎样 哪些 一个 这些 那些 时间 东西 事情 方法 方式 情况 内容
""".split())

# 候选侧字段权重(spec §2.4):锚点在字段出现即 matched,取最高字段权;
# coverage 里 min(1, ·) 封顶——node_terms 的 1.20 是"优于满分字段"的排序信号,不放大 coverage。
_FW_NODE_TERMS = 1.20
_FW_OVERVIEW = 1.00
_FW_HIGHLIGHTS = 1.00
_FW_SUMMARY = 0.75
_FW_SOURCE = 0.35


@dataclass(frozen=True)
class EpisodeCandidate:
    """重排输入:调用方从候选池行 + 双路名次 + 现算活跃度组装,一池 ≤24 条。"""

    eid: int                 # 内部 id,仅供调用方回查,不对外输出(红线在调用方守)
    public_id: str
    overview: str
    summary: str
    highlights: list[str]    # highlight 文本列表(调用方已从 json 解出 text 字段)
    source_text: str
    node_terms: list[str]    # 该候选挂的 node label + alias(全部)
    created_at: str
    vector_rank: int | None  # 向量路名次(1-based),未命中 None
    fts_rank: int | None     # 整句 FTS 路名次,未命中 None
    activation: float        # 调用方现算好的 effective_activation


@dataclass(frozen=True)
class Anchor:
    text: str
    weight: float      # 参与 coverage 的权重
    idf_proxy: float   # 具体度近似,参与 query_specificity


@dataclass(frozen=True)
class RankingResult:
    ordered: list[int]              # 全体候选 eid,按 relevance 降序(tie-break 后)
    scores: dict[int, float]        # eid -> relevance(clamp 前原始值)
    features: dict[int, dict]       # eid -> 各特征分解(debug/eval 用)
    anchors: list[Anchor]           # 本 query 抽出的锚点(debug 用)


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _cjk_runs(text: str) -> list[str]:
    """连续 CJK 串按非 CJK 字符切开,返回各段(保持出现顺序)。"""
    runs: list[str] = []
    buf: list[str] = []
    for ch in text:
        if _is_cjk(ch):
            buf.append(ch)
        elif buf:
            runs.append("".join(buf))
            buf = []
    if buf:
        runs.append("".join(buf))
    return runs


def _demote(text: str, weight: float, idf: float) -> tuple[str, float, float]:
    """整段/bigram 命中停用词表 → 覆盖为降权值;其余原样。"""
    if text in _STOPWORDS:
        return (text, _W_STOP, _IDF_STOP)
    return (text, weight, idf)


def extract_anchors(query: str, node_terms: Iterable[str]) -> list[Anchor]:
    """确定性锚点抽取(spec §2.3):按类型序抽取,再按 text 去重取最高权。

    - node/alias:node_terms 中长度≥2 且是 query 子串的项,区分大小写按原样;
      词表排序后遍历,保证 Iterable 传 set 也可重放。
    - 英文/代码 token:统一 casefold 存储(匹配侧同样 casefold)。
    - 数字/指标:长度≥2(裸个位数不算证据)。
    - 中文 run:2~6 字出整段;>6 只出 bigram(太长的整段是 phrase,交给 FTS 路);
      整段与 bigram 命中停用词表则降权。
    - query 无任何锚点 → 空列表(下游 coverage=0、gap=0,退化为 RRF+activation 序)。
    """
    q = query or ""
    raw: list[tuple[str, float, float]] = []
    for term in sorted(set(node_terms)):
        if len(term) >= 2 and term in q:
            raw.append((term, _W_NODE, _IDF_NODE))
    for m in _EN_TOKEN.finditer(q):
        raw.append((m.group().casefold(), _W_EN, _IDF_EN))
    for m in _NUM_TOKEN.finditer(q):
        if len(m.group()) >= 2:
            raw.append((m.group(), _W_NUM, _IDF_NUM))
    for run in _cjk_runs(q):
        if 2 <= len(run) <= 6:
            raw.append(_demote(run, _W_RUN, _IDF_RUN))
        if len(run) >= 2:
            for i in range(len(run) - 1):
                raw.append(_demote(run[i:i + 2], _W_BIGRAM, _IDF_BIGRAM))
    # 同 text 多来源:取 weight/idf 最高的一条;输出保持首次出现顺序(debug 可读)。
    best: dict[str, tuple[float, float]] = {}
    order: list[str] = []
    for text, w, idf in raw:
        if text not in best:
            best[text] = (w, idf)
            order.append(text)
        elif (w, idf) > best[text]:
            best[text] = (w, idf)
    return [Anchor(text=t, weight=best[t][0], idf_proxy=best[t][1]) for t in order]


def _anchor_in(anchor_text: str, field: str) -> bool:
    """锚点是否出现在字段。英文锚点抽取时已 casefold,故 text==casefold 时补一次
    casefold 匹配(spec §2.4);node/alias 含大写按原样,不做二次尝试。CJK 不受影响。"""
    if not field:
        return False
    if anchor_text in field:
        return True
    if anchor_text == anchor_text.casefold():
        return anchor_text in field.casefold()
    return False


def _matched(anchor: Anchor, c: EpisodeCandidate) -> float:
    """matched(a, c) = 命中字段的最高权重,未命中 0。按权重降序查,首中即返。"""
    if any(_anchor_in(anchor.text, t) for t in c.node_terms):
        return _FW_NODE_TERMS
    if _anchor_in(anchor.text, c.overview):
        return _FW_OVERVIEW
    if any(_anchor_in(anchor.text, h) for h in c.highlights):
        return _FW_HIGHLIGHTS
    if _anchor_in(anchor.text, c.summary):
        return _FW_SUMMARY
    if _anchor_in(anchor.text, c.source_text):
        return _FW_SOURCE
    return 0.0


def _anchor_coverage(anchors: list[Anchor], c: EpisodeCandidate) -> float:
    """Σ_a weight_a·min(1, matched(a,c)) / Σ_a weight_a;无锚点 → 0。"""
    total = sum(a.weight for a in anchors)
    if total <= 0.0:
        return 0.0
    hit = sum(a.weight * min(1.0, _matched(a, c)) for a in anchors)
    return hit / total


def _query_specificity(anchors: list[Anchor]) -> float:
    """min(1, 0.25·log1p(锚点数) + 0.75·mean(idf_proxy));无锚点 → 0。"""
    if not anchors:
        return 0.0
    mean_idf = sum(a.idf_proxy for a in anchors) / len(anchors)
    return min(1.0, 0.25 * math.log1p(len(anchors)) + 0.75 * mean_idf)


def _rrf_norm(c: EpisodeCandidate, rrf_k: int) -> float:
    """RRF 只用名次(与 episode.py ④ 同式),再归一到双路第一名 = 1。"""
    raw = 0.0
    if c.vector_rank is not None:
        raw += 1.0 / (rrf_k + c.vector_rank)
    if c.fts_rank is not None:
        raw += 1.0 / (rrf_k + c.fts_rank)
    return raw / (2.0 / (rrf_k + 1))


def rank_episode_candidates(
    query: str,
    candidates: list[EpisodeCandidate],
    *,
    rc: RecallConfig,
    node_terms: Iterable[str],
) -> RankingResult:
    """对整个候选池做线性重排,返回全体 eid 降序 + 分解特征。

    `node_terms` 是 query 侧锚点词表(全库 node label + alias),与候选各自挂的
    `c.node_terms` 不同轴:前者决定"query 里有什么锚点",后者是候选侧最强匹配字段。
    排序 key `(-relevance, created_at, public_id)`,与主槽现行 tie-break 一致(可重放)。
    """
    anchors = extract_anchors(query, node_terms)
    specificity = _query_specificity(anchors)
    scores: dict[int, float] = {}
    features: dict[int, dict] = {}
    by_eid: dict[int, EpisodeCandidate] = {}
    for c in candidates:
        by_eid[c.eid] = c
        rrf_n = _rrf_norm(c, rc.rrf_k)
        cov = _anchor_coverage(anchors, c)
        gap = specificity * (1.0 - cov)
        rel = (rc.w_rank_rrf * rrf_n + rc.w_rank_anchor * cov
               + rc.w_rank_activation * c.activation - rc.w_rank_gap * gap)
        scores[c.eid] = rel
        features[c.eid] = {"rrf_norm": rrf_n, "anchor_coverage": cov, "gap": gap,
                           "activation": c.activation, "relevance": rel}
    ordered = sorted(
        scores,
        key=lambda e: (-scores[e], by_eid[e].created_at, by_eid[e].public_id))
    return RankingResult(ordered=ordered, scores=scores,
                         features=features, anchors=anchors)
