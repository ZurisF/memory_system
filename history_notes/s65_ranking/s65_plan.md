# S6.5 轻量召回排序 — 施工 spec(定稿)

仓库:`/Users/zuris/Workspace/memory_system`(git 干净,基线 d11d894)。
所有验证用项目 venv:`.venv/bin/python`。禁止装任何新依赖、禁止 git commit。
代码风格:跟随现有文件——中文 docstring/注释、注释只写约束和裁定不写废话、
所有排序带 tie-break(public_id/created_at)保证同库同 query 可重放。

## 0. 背景(已由数据钉死,不要复查)

- 召回评测基线 `/tmp/a.json`:episode/semantic hit@3 = 46.2%,hit@1 = 17.6%。
- 病灶已定位:episode 的 134 条 miss 里 96.3% 的期望条目**已在原始 12 条候选池内**,
  排在 4~12 名;扩池收益仅 3.7%。纯排序问题。
- 原因:FTS 路是整句 phrase(`_fts_phrase`),semantic 改写查询下 FTS 空手,
  RRF 退化为纯向量序 × 激活度乘子(w_activation=0.3 最多放大 1.3 倍),
  泛化条目(如 ep_syn0169)凭 dense 相近 + 活跃霸榜。
- 对策:**不加召回通道、不扩池**。对现有候选池做轻量特征重排:
  确定性锚点抽取 + anchor_coverage + specificity_gap,activation 降为弱加项。
  MMR / generic_penalty / df 表 / anchor FTS 全部不做(v1 冰箱)。

## 1. 铁律(ARCHITECTURE.md §2,违反即返工)

1. 碎片是正本,SQLite 可重建——本次不动 schema、不动碎片格式。
2. 对外输出手工挑字段,只 public_id / node label,无 uuid/向量/DB 整数 id。
3. key 只从环境读。本次不涉及。

## 2. Step 1:`memory_system/recall/ranking.py`(新文件)+ config 权重

### 2.1 RecallConfig 新字段(config.py)

在 `RecallConfig` 追加(注释说明语义),并在 `_recall_from_env` 用 `_env_float`
(坏值回落)接 `MEMORY_RECALL_*` 环境变量:

```python
w_rank_rrf: float = 0.40         # 线性重排:RRF 归一分权重(语义底盘)
w_rank_anchor: float = 0.40      # 线性重排:锚点覆盖权重(本次核心)
w_rank_gap: float = 0.18         # 线性重排:specificity gap 惩罚权重
w_rank_activation: float = 0.05  # 线性重排:活跃度弱加项(仅 tie-break 级)
```

`w_activation`(旧乘子)字段保留不删——opening/其他消费者不受影响;
episode 新排序不再使用它。

### 2.2 ranking.py 设计

零依赖、纯函数、不碰 DB、不 import embedding/agent。模块 docstring 写清裁定:
「候选池已含 96% 正确答案,本模块只负责最后一米的排序;锚点=确定性规则,无 LLM」。

数据结构:

```python
@dataclass(frozen=True)
class EpisodeCandidate:
    eid: int                 # 内部 id,仅供调用方回查,不对外输出
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
```

对外接口:

```python
def extract_anchors(query: str, node_terms: Iterable[str]) -> list[Anchor]: ...
def rank_episode_candidates(query: str, candidates: list[EpisodeCandidate],
                            *, rc: RecallConfig, node_terms: Iterable[str]) -> RankingResult: ...
```

### 2.3 锚点抽取规则(确定性,按序抽取后按 text 去重取最高权)

| 类型 | 规则 | weight | idf_proxy |
|---|---|---:|---:|
| node/alias | `node_terms` 中长度≥2 且是 query 子串的项(区分大小写按原样) | 1.4 | 1.00 |
| 英文/代码 token | 正则 `[A-Za-z][A-Za-z0-9_.+#/@-]{1,}`,匹配统一 casefold | 1.3 | 0.90 |
| 数字/指标 | 正则 `\d+(?:[.:∶]\d+)*%?`,长度≥2 | 1.2 | 0.85 |
| 中文整段 run | 连续 CJK 串按非 CJK 切开,长度 2~6 的整段 | 1.1 | 0.70 |
| 中文 bigram | 长度≥2 的 CJK run 内 stride-1 二字滑窗 | 1.0 | 0.55 |
| 停用词降权 | 上两类若命中 `_STOPWORDS` → 覆盖为 | 0.3 | 0.15 |

`_STOPWORDS`(模块常量 frozenset,可后续增补):
系统 项目 方案 问题 优化 处理 讨论 之前 那个 这个 时候 怎么 什么 如何 我们 你们
今天 昨天 记得 提到 说过 聊过 关于 一下 可以 应该 需要 现在 后来 当时 觉得 还是
是不是 有没有 怎样 哪些 一个 这些 那些 时间 东西 事情 方法 方式 情况 内容

细则:
- CJK 判定:`'一' <= ch <= '鿿'`。
- run 长度 >6 只出 bigram,不出整段锚点(太长的整段是 phrase,交给 FTS 路)。
- 同 text 多来源:取 weight/idf 最高的一条。
- query 无任何锚点 → 返回空列表(下游 coverage=0、gap=0,退化为 RRF+activation 序)。

### 2.4 特征与线性分

候选侧字段权重(锚点在该字段出现即 matched,取最高字段权;英文锚点 casefold 匹配):

| 字段 | 权重 |
|---|---:|
| node_terms(逐项) | 1.20 |
| overview | 1.00 |
| highlights(逐条) | 1.00 |
| summary | 0.75 |
| source_text | 0.35 |

```text
matched(a, c)     = max(field_weight | anchor a 是该字段子串), 未命中 = 0
anchor_coverage   = Σ_a weight_a · min(1, matched(a,c)) / Σ_a weight_a   # 无锚点 → 0
rrf_raw           = Σ_路 1/(rrf_k + rank_路)      # 与现 episode.py ④ 相同
rrf_norm          = rrf_raw / (2/(rrf_k + 1))     # 双路第一名 = 1
query_specificity = min(1, 0.25·log1p(锚点数) + 0.75·mean(idf_proxy))   # 无锚点 → 0
gap               = query_specificity · (1 − anchor_coverage)
relevance         = w_rank_rrf·rrf_norm + w_rank_anchor·anchor_coverage
                  + w_rank_activation·activation − w_rank_gap·gap
```

排序 key:`(-relevance, created_at, public_id)`(与现主槽 tie-break 一致)。
features 里给每候选记 `rrf_norm / anchor_coverage / gap / activation / relevance`。

### 2.5 Step 1 验证:`scripts/verify_ranking.py`(新文件,离线,不碰真实库)

跟随 verify_s6.py 的 `ok()` 风格。至少断言:
1. 锚点抽取:混合 query(如「处理 IEEE 论文 PDF 时分块老把公式拆开,recall@10 掉到 0.85」+
   node_terms 含「文档分块」)抽出 node/alias、英文、数字、bigram 各类,权重正确;
   停用词 bigram 被降权;去重取最高权。
2. 旗舰场景:两候选同 vector_rank 相邻(泛化条目 rank 1、具体条目 rank 2),
   具体条目覆盖高权锚点 → 重排后具体条目在前。
3. 泛 query(锚点少且弱)下 gap 惩罚接近 0,RRF 序保持(不误杀综合条目)。
4. 无锚点 query 退化:排序 = RRF+activation 序。
5. 确定性:同输入两次调用结果逐字段相等;relevance 相同(人工构造)时按
   created_at/public_id 定序。
6. 全部候选 features 键齐全,数值在预期区间([0,1] 或文档化范围)。

跑 `.venv/bin/python scripts/verify_ranking.py` 全绿;`python -m py_compile` 过。

### 2.6 Step 1 完成产物(交接)

- 代码:ranking.py、config.py 改动、verify_ranking.py。
- 手写交接:`/tmp/s65_step1_handoff.md` —— 实际接口签名、与本 spec 的任何偏差及理由、
  verify 输出摘要。**Step 2 只看磁盘,不看你的对话。**

## 3. Step 2:episode.py 接线(读 /tmp/s65_step1_handoff.md 后动工)

改 `memory_system/recall/episode.py` 的 ④⑤⑥ 段:

1. 候选行现已取 `overview/summary/highlights_json/source_text/created_at/...`;
   补两个批量查询(池 ≤24 条,IN 查询):
   - 每候选的 node label+alias:`episode_nodes → nodes → node_aliases`(LEFT,无别名也要 label);
   - query 侧锚点词表:全库 `SELECT label FROM nodes` + `SELECT alias FROM node_aliases`
     (nodes 表小,直接全量;这是 query 侧证据源,与候选无关)。
2. 组装 `EpisodeCandidate` 列表(activation 用现有 `decay.effective_activation` 现算,
   与旧代码同参),调 `rank_episode_candidates`。
3. 主槽 = `result.ordered[:topk_final]`,`score` 字段 = `round(relevance, 6)`
   (对外契约字段名不变)。
4. **冷却语义保持**:cooldown 命中者 `relevance` 先 `max(0, ·)` 再乘 `cooldown_factor`
   (负分直接乘会把分抬高,这是 clamp 的原因,注释写明);冷却日志行为不变。
   dedup 硬排除仍在进池前做,位置不动。
5. 同源槽/联想槽/别名桥接/touch/injected_log 全部不动。
6. 旧的 `final = rrf * (1 + w_activation * act)` 路径删除(不留开关;git 即回滚)。

### Step 2 验证

- `.venv/bin/python scripts/verify_s6.py` 必须全绿。断言若因分数**刻度**失效
  (如具体分值比较),可最小限度改断言;**语义断言不许放松**:双路命中排单路前、
  dedup 硬排除、cooldown factor<1 翻转相邻序且 =1.0 还原、tie-break 可重放、
  红线(无 uuid/向量/DB id)。每处改动在交接文档里说明理由。
- `verify_ranking.py` 仍绿;`scripts/eval_recall.py --selftest` 仍绿(它走 fake 向量与
  episode 路径)。
- ARCHITECTURE.md §5.10 episode 一条更新管线描述(RRF → 轻量特征重排:锚点覆盖 +
  specificity gap,activation 弱加项;一两句即可,风格跟随)。
- 交接:`/tmp/s65_step2_handoff.md`(改了哪几段、verify 结果、断言改动清单)。

## 4. Step 3:detail.py 中文短词 fallback(读 step2 交接后动工,别碰 ranking/episode)

`recall_detail`:FTS 查询结果为空(含 OperationalError)**且** `len(q) < 3` 时,
走 LIKE/instr 子串回退(detail 本就是 grep 语义,子串扫描不违背设计):

```sql
SELECT id, public_id, created_at, salience_tier, source_text,
       (length(source_text) - length(replace(source_text, :q, ''))) / length(:q) AS occ
FROM episodes
WHERE status='active' AND instr(source_text, :q) > 0 [AND created_at >=/<= ...]
ORDER BY occ DESC, created_at DESC LIMIT :lim
```

- 开窗:Python 侧,首次出现位置前后各 `window_tokens` 字符,截断端加 `…`;
  `--raw` 仍整条原文。契约字段不变。
- 命中刷时钟,与 FTS 路一致。
- ≥3 字空手**不**回退(语义:真没有;避免长 query 改性)。
- 模块 docstring 第 9 行「中文短词 trigram 不可靠」的说法更新;
  ARCHITECTURE.md §5.10 detail 条「中文查询 ≥3 字」更新;CLI 若有「换更长的词」
  提示文案(查 cli.py)相应弱化。
- 验证:verify_s6 seg_s6_2 增补断言(2 字词命中 + 窗口含词 + occ 排序 + since 过滤
  仍生效 + 时钟刷新;3 字以上空手行为不变)。verify_s6 全绿。
- 交接:`/tmp/s65_step3_handoff.md`。

## 5. Step 4:全量评测 + 粗调(主对话自己跑,不派工)

基线 `/tmp/a.json`。护栏:detail/verbatim hit@3 ≥ 0.88 不回退、concept 不回退。
目标:episode/semantic hit@3 46%→60%+,hit@1 显著抬升;short 直接看翻身幅度。
粗调网格(--param):w_rank_anchor ∈ {0.30,0.40,0.50},w_rank_gap ∈ {0.10,0.18,0.26},
w_rank_rrf 固定 0.40,先单变量后组合,报告写 /tmp/s65_eval_report.md。
