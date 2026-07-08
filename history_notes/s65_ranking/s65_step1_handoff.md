# S6.5 Step 1 交接(2026-07-08)

基线 d11d894 之上,未 commit。改动三个文件:

- 新建 `memory_system/recall/ranking.py`(纯函数、零依赖,只 import stdlib + `memory_system.config.RecallConfig` 做类型)
- 改 `memory_system/config.py`(RecallConfig 追加四字段 + `_recall_from_env` 接四个环境变量)
- 新建 `scripts/verify_ranking.py`(spec §2.5 六组断言,离线)

## 1. 实际接口签名(ranking.py)

```python
@dataclass(frozen=True)
class EpisodeCandidate:
    eid: int
    public_id: str
    overview: str
    summary: str
    highlights: list[str]     # 调用方已从 highlights_json 解出的 text 列表
    source_text: str
    node_terms: list[str]     # 该候选挂的 node label + alias(全部)
    created_at: str
    vector_rank: int | None   # 1-based,未命中 None
    fts_rank: int | None
    activation: float         # 调用方现算好的 effective_activation

@dataclass(frozen=True)
class Anchor:
    text: str
    weight: float
    idf_proxy: float

@dataclass(frozen=True)
class RankingResult:
    ordered: list[int]        # 全体候选 eid,relevance 降序 + tie-break(-rel, created_at, public_id)
    scores: dict[int, float]  # eid -> relevance(clamp 前原始值;Step 2 冷却时 max(0,·) 再乘 factor)
    features: dict[int, dict] # eid -> {rrf_norm, anchor_coverage, gap, activation, relevance}
    anchors: list[Anchor]

def extract_anchors(query: str, node_terms: Iterable[str]) -> list[Anchor]: ...
def rank_episode_candidates(query: str, candidates: list[EpisodeCandidate],
                            *, rc: RecallConfig, node_terms: Iterable[str]) -> RankingResult: ...
```

`rank_episode_candidates` 的 `node_terms` 形参是 **query 侧词表**(全库 node label + alias),
与候选各自的 `c.node_terms`(匹配字段,权重 1.20)不同轴——Step 2 接线时别传混。
公式、锚点权重表、字段权重表、_STOPWORDS 与 spec §2.3/§2.4 逐字一致。
`rrf_norm` 用 `rc.rrf_k`,与 episode.py ④ 同式后除以 `2/(rrf_k+1)` 归一。

## 2. config.py 改动

`RecallConfig` 在 `w_activation` 后追加(默认值即 spec 值):

```python
w_rank_rrf: float = 0.40
w_rank_anchor: float = 0.40
w_rank_gap: float = 0.18
w_rank_activation: float = 0.05
```

`_recall_from_env` 用 `_env_float`(坏值回落)接:
`MEMORY_RECALL_W_RANK_RRF / W_RANK_ANCHOR / W_RANK_GAP / W_RANK_ACTIVATION`。
已冒烟:override 生效、坏值(非数字)回落默认。`w_activation` 旧字段原样保留。

## 3. 与 spec 的偏差(3 处,均为 spec 未明说处的落地裁定)

1. **英文锚点 casefold 匹配的判定**:Anchor 无类型字段(spec 定死三字段),匹配侧用
   `_anchor_in`:先原样子串;若 `text == text.casefold()` 再补一次双方 casefold 匹配。
   效果:英文 token(抽取时已 casefold)得到 casefold 匹配;含大写的 node/alias
   锚点保持"按原样"不做二次尝试;CJK/数字不受影响。
2. **extract_anchors 内 node_terms 先 `sorted(set(...))`**:形参是 Iterable,调用方
   传 set 时锚点输出顺序也可重放(verify seg_5 有断言)。去重结果不受影响。
3. **数字正则不遮蔽英文 token 范围**:各正则独立扫原 query,如 `recall@10` 会同时出
   英文锚点 `recall@10`(1.3)和数字锚点 `10`(1.2)。spec 未要求遮蔽;两锚点在候选侧
   通常同时命中或同时不命中,对 coverage 是轻微平滑,不改变序性质。

无其他偏差:数据结构、公式、权重、tie-break、`_STOPWORDS` 均照 spec 定稿。

## 4. verify_ranking.py 输出摘要

`.venv/bin/python scripts/verify_ranking.py` 全绿(py_compile 亦过):

- seg_1 锚点抽取:17 锚点 text 唯一;node/alias 1.4(「文档分块」非子串不出);
  英文 casefold 1.3(pdf 与 node「PDF」并存);数字 0.85→1.2;整段 1.1 / bigram 1.0;
  9 字 run 无整段;停用词「处理」→0.3/0.15;单字/空 query → 空列表。
- seg_2 旗舰:具体条目(vec_rank=2, act=0.2, coverage=1.0)relevance 0.6068 ≫
  泛化条目(vec_rank=1, act=1.0, coverage=0, gap=1.0)0.0700,反超成立。
- seg_3 泛 query(纯停用词锚点):gap 各候选同值,w_gap·gap = 0.0826 < 0.1,RRF 序保持。
- seg_4 无锚点:anchors 空、coverage/gap 全 0,relevance 逐候选核对
  = w_rrf·rrf_norm + w_act·act;activation 弱加项翻越相邻 rrf 名次(0.2467 > 0.2)。
- seg_5 确定性:同输入(词表传 set)两次调用四字段逐一相等;同分按
  created_at 升序、再 public_id 升序(23→21→22)。
- seg_6:features 五键齐全,各特征 ∈[0,1],relevance ∈ [-0.18, 0.85],
  scores == features["relevance"],ordered 覆盖全池;双路第一名 rrf_norm = 1.0。

另跑 `scripts/verify_s6.py` 回归:ALL PASS(config 改动无副作用)。

## 5. 给 Step 2 的提醒

- 主槽 score = `round(result.scores[eid], 6)`;冷却 clamp 语义见 spec §3.4。
- highlights 记得从 `highlights_json` 解出 **text 字段**(元素若是 dict)再进
  `EpisodeCandidate.highlights`;ranking 只做子串匹配,不解析 json。
- features/anchors 只供 debug/eval,不进对外契约。
