# S6.5 Step 3 交接(2026-07-08)

Step 2 基线之上,未 commit。没碰 ranking.py / episode.py / config.py / `_fts_phrase` 本体。
改动四个文件:

- `memory_system/recall/detail.py`(instr 回退 + Python 开窗 + docstring)
- `memory_system/cli.py`(空结果提示文案一处)
- `ARCHITECTURE.md`(§5.10 detail 条「中文查询 ≥3 字」改为回退说明)
- `scripts/verify_s6.py`(seg_s6_2 尾部增补断言 (7)~(12) + 头部 S6-2 摘要行;其余段零改动)

## 1. detail.py 改动摘要

1. **回退分支**:FTS 查询 rows 为空(含 OperationalError 已归空)且 `len(q) < 3` 时,
   走 `instr(e.source_text, ?) > 0` 子串查询;
   `occ = (length(src) - length(replace(src, q, ''))) / length(q)`(SQLite length 按字符数,
   整除精确),`ORDER BY occ DESC, e.created_at DESC LIMIT :lim`(lim 与 FTS 路同源)。
   since/until 追加进回退 WHERE,占位符按 SQL 文本序排列(occ 两个 → instr 一个 →
   since/until → LIMIT,代码有注释)。
2. **开窗**:新增 `_substr_window(text, q, window)` —— `text.find(q)` 首次出现位置前后各
   `rc.window_tokens` 字符,`start>0` 前缀 `…`、`end<len` 后缀 `…`;`raw=True` 仍整条
   source_text(判断顺序 raw → fallback → FTS window)。
3. **契约与时钟**:hits 字段仍是 public_id/window/created_at/salience_tier 四键;
   hit_ids 收集与 touch/commit 走原有同一段代码,回退命中 touch 语义与 FTS 路完全一致。
4. **≥3 字空手不回退**(fallback 条件里的 `len(q) < 3`),模块 docstring 第 9 条与
   recall_detail docstring 同步更新说法。

## 2. cli.py / ARCHITECTURE.md

- cli.py `_recall_detail` 空结果提示:原「FTS trigram 对少于 3 个字符的中文词不可靠,
  换更长/更具体的词再试」→「库内原文没有逐字包含该词的段落(<3 字短词已自动走子串回退),
  换更具体的词或调整时间窗再试」。
- ARCHITECTURE.md §5.10 detail 条:「中文查询 ≥3 字(trigram 实测边界)」→
  「中文短词 <3 字 FTS 空手时自动降级 instr 子串回退(S6.5:出现次数降序 + created_at 降序,
  Python 侧开窗,时钟/契约同 FTS 路);≥3 字空手不回退(真没有)」。

## 3. verify_s6.py seg_s6_2 增补(全部长在段尾,原 (1)~(6) 一字未动)

- (7) 2 字词「松饼」FTS 空手 → 回退命中 ep_bbbb0002,窗口含词,契约四键不变。
- 临时语料 FB1(咖啡 ×3,6/5)/FB2(咖啡 ×1,6/25)写碎片 + rebuild(episodes=5);
  **段尾 unlink 两个碎片自清**,seg_s6_3 开头 rebuild 的 `episodes == 11` 计数不受影响
  (rebuild 是 DELETE FROM episodes 全量重建,DB 自动收敛)。
- (8) occ 排序:FB1(旧、×3)压过 FB2(新、×1);touch=False 探查不刷时钟。
- (9) 开窗:window_tokens=2 的小窗下窗口含词且两端都有 …;--raw 回退路仍整条原文。
- (10) since=2026-06-10 在回退路生效,只剩 FB2。
- (11) 默认 touch:FB1/FB2 刷到 NOW,未命中 A 不动(基线 != NOW 防空转断言)。
- (12) ≥3 字空手行为不变:「量子茶壶」(4 字)空 hits;「茶壶」(2 字)回退扫描后同样空。
- 另:文件头 docstring 的 S6-2 摘要行补了回退覆盖说明(纯文档)。

## 4. verify 结果(全部 .venv/bin/python)

- `scripts/verify_s6.py` → `S6 检索层 ALL PASS ✅`(含新增 6 条 [ok];其余段断言零改动)
- `scripts/eval_recall.py --selftest` → `[selftest] PASS: 3/3 正例命中且均居 rank 1(MRR=1.0)…`
- `scripts/verify_ranking.py` → `verify_ranking: 全部通过`(顺手回归,确认没碰坏 Step 1/2)
- `python -m py_compile` detail.py / cli.py / verify_s6.py 全过

## 5. 给 Step 4 的提醒

- 回退只挂在 recall_detail 的空手分支,`_fts_phrase` 与 episode.py 的 FTS 路零变化,
  episode 评测数字不会因本步改变。
- detail 评测里若有 <3 字中文 query,现在会经回退命中——detail/verbatim 护栏(hit@3 ≥ 0.88)
  只可能升不可能降;若真降了先查回退 occ 排序是否把预期条目挤出 limit。
