# S6.5 Step 2 交接(2026-07-08)

基线 d11d894 + Step 1 改动之上,未 commit。本步只改两个文件:

- `memory_system/recall/episode.py`(④⑤⑤b⑥ 段接线 ranking)
- `ARCHITECTURE.md`(§5.10 episode 一条管线描述,两句)

不碰 detail.py、不碰 ranking.py(未发现 ranking.py bug,公式零改动)。

## 1. episode.py 改动摘要

1. **模块 docstring**:标题行与十要点的第 4/5 条改为「轻量特征重排(S6.5,ranking.py)」;
   recall_episode docstring 里冷却语义措辞同步(「relevance 钳非负后乘 cooldown_factor」)。
2. **import**:`from memory_system.recall.ranking import EpisodeCandidate, rank_episode_candidates`。
3. **④+⑤ 合并为重排段**(原 `final = rrf * (1 + w_activation * act)` 循环整体删除,不留开关):
   - 候选侧 node 词表:`episode_nodes JOIN nodes LEFT JOIN node_aliases WHERE episode_id IN (...)`
     一次批量查询,label 去重收集、alias 非 NULL 才收(LEFT 保证无别名也有 label)。
   - query 侧词表:全库 `SELECT label FROM nodes` + `SELECT alias FROM node_aliases`,
     与候选侧不同轴(注释写明,照 Step 1 交接提醒)。
   - 组装 `EpisodeCandidate`:highlights 从 `_highlights(r)`(json 已解)取每元素的
     `text` 字段(dict)或 `str(h)` 兜底;activation 用 `decay.effective_activation`
     同参现算(last_accessed_at/salience_tier/rc/now + activated_at/created_at,与旧代码逐参一致);
     vector_rank/fts_rank 直接 `dict.get`(未命中 None)。
   - `final = dict(ranked.scores)`;features/anchors 不外泄(注释点名)。
   - 整段包在 `if active:` 里,active 空时 final 空、行为与旧代码一致(空池早退在更上游)。
4. **⑤b 冷却**:`final[eid] = max(0.0, final[eid]) * rc.cooldown_factor`,注释写明
   relevance 可为负(gap 惩罚),负分直乘 factor<1 反而抬分,先钳 0 再乘保证只降不升。
   cooldown_pids 的计算、日志行为、dedup 硬排除(③ 进池前)位置全部未动。
5. **⑥ 主槽**:仍是 `sorted(final, key=(-分, created_at, public_id))[:topk_final]`——
   key 与 `RankingResult.ordered` 的 tie-break 完全同式,冷却未触发时结果逐位等于
   `ranked.ordered[:topk_final]`(注释写明);统一走一条排序路径是为了冷却改分后
   不需要二次分支。`score` 字段 = `round(final[i], 6)`(即 relevance,冷却后值),
   字段名与契约形状不变。
6. 同源槽/联想槽/别名桥接/touch/injected_log 逐行未动。

## 2. verify 三项结果(全部 .venv/bin/python)

- `scripts/verify_s6.py` → `S6 检索层 ALL PASS ✅`
- `scripts/verify_ranking.py` → `verify_ranking: 全部通过`
- `scripts/eval_recall.py --selftest` → `[selftest] PASS: 3/3 正例命中且均居 rank 1(MRR=1.0)…`

另:`python -m py_compile memory_system/recall/episode.py` 过。

## 3. 断言改动清单

**零改动。** verify_s6.py 一行未碰:seg_s6_3 的双路压单路、三槽形状、时钟、FTS 空手、
红线,seg_s6_p2_1 的去重/冷却翻转/factor=1.0 还原/窗口外/touch=False/红线,全部在
新排序下天然成立。原因:
- seg_s6_3 语料里 D 双路命中且 coverage=1.0,F/G(overview=Q1)coverage 同为 1.0,
  高下仍由 rrf_norm 决出(双路 > 单路),D 稳居 top-1;
- 冷却翻转断言取运行时 top1/top2 动态比较,不吃分数刻度(本次 top2 恰为 ep_gggg0007,
  factor=0.01 下 max(0,rel)·0.01 远低于 top2 的 relevance,翻转成立);
- factor=1.0 时 cooldown_pids 按现有守卫(`cooldown_factor != 1.0`)根本不收集,
  clamp 不会执行,还原断言不受 max(0,·) 影响。

## 4. 给 Step 3(detail.py)的提醒

- 本步没碰 detail.py;`_fts_phrase` 仍被 episode.py 引用,改它的行为会影响 episode
  的 FTS 路,fallback 请只加在 recall_detail 的空手分支,别动 `_fts_phrase` 本体语义。
- verify_s6 全程用临时 home + fake embedding,新增断言直接往 seg_s6_2 里长即可。
