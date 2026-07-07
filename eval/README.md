# 召回评测:批量生产运行手册

> 语料和 query 由你用便宜模型(下称 mimo)按 `prompts/` 里的两份 system prompt 批量生成;
> 本目录的工具链负责校验、落库、评测、查看。设计裁定见 `project/eval_build_plan.md`。

## 目录约定

```
eval/
  prompts/gen_corpus_system.txt    # mimo 生成语料的 system prompt
  prompts/gen_queries_system.txt   # mimo 出题的 system prompt
  raw/corpus.jsonl                 # mimo 产物:语料(你自己攒,分批 append)
  raw/query_inputs/<簇>.jsonl      # prep-queries 生成的出题输入(工具产)
  raw/queries_raw.jsonl            # mimo 产物:题目(临时 id 版)
  queries.jsonl                    # ingest 改写后的正式夹具(真 public_id)
  manifest.json                    # ingest 落的溯源锚(条数/id 映射/anchor/embedding 模型)
```

## 完整流程

### ① 生成语料(eval_gen 驱动 mimo)

```bash
.venv/bin/python scripts/eval_gen.py corpus                 # 按 eval/clusters.jsonl 全部簇生成
.venv/bin/python scripts/eval_gen.py corpus --cluster mem   # 只生成一簇
```

key 走环境变量 `mimo_api_key`,模型默认 `mimo-v2.5`(`--model mimo-v2.5-pro` 可换)。
脚本按簇分批(每批 5 条)调用、逐行校验(字段齐/ id 唯一/ highlights 逐字子串),坏行落
`eval/raw/rejects.log` 不污染语料;**断点续跑**——同一命令可反复执行,已有 id 自动跳过,
跑到每簇显示「待生成 0」即补齐(便宜模型有 3–5 成坏行率是正常的,重跑补上就行)。

簇清单正本是 `eval/clusters.jsonl`(脚本读它,增删簇改它);下表是同一份清单的人读版:

**现成簇清单**(约 190 条,近邻成对设计,可自行增删):

| 前缀 | 主题 | 条数 | 近邻 |
|---|---|---|---|
| mem | 持久化记忆系统开发(切段/蒸馏/向量检索/概念图) | 20 | ↔ rag |
| rag | 另一个 RAG 问答机器人项目(分块/重排/幻觉治理) | 20 | ↔ mem |
| pet | 桌面宠物应用开发(Tauri/动画/系统托盘) | 15 | ↔ mem(同为个人项目) |
| cook | 下厨实验与翻车记录 | 20 | ↔ diet |
| diet | 饮食调整与体检指标 | 15 | ↔ cook |
| scifi | 科幻小说创作讨论(设定/大纲/人物弧) | 20 | ↔ trpg |
| trpg | 跑团战役(角色/剧情推进/规则争议) | 20 | ↔ scifi |
| phil | 存在主义与身份哲学闲聊 | 15 | ↔ ai |
| ai | AI 意识与对齐话题闲聊 | 15 | ↔ phil |
| trip | 旅行规划与途中见闻 | 15 | 独立 |
| work | 工作琐事与职场决定 | 15 | 独立 |

> 配比说明:近邻簇是评测的主菜(检索器要在"都谈检索工程"的 40 条里挑对那条);
> 独立簇是背景噪声。总量 150–300 都行,先跑 190 看指标再加。
>
> 手动备选(不走脚本):把 `prompts/gen_corpus_system.txt` 作 system 喂给任意模型,
> user 给一份簇规格(簇前缀/主题/条数/近邻关系,照 clusters.jsonl 的字段口述),
> 输出追加到 `eval/raw/corpus.jsonl` 后自己跑 `eval_ingest.py validate` 把关。

### ② 校验语料

```bash
.venv/bin/python scripts/eval_ingest.py validate eval/raw/corpus.jsonl
```

报错带行号(id 重复 / highlights 不是逐字子串 / 字段缺失等)。坏行让 mimo 重生成或手修。

### ③ 生成出题输入(工具,免得 query 抄 overview)

```bash
.venv/bin/python scripts/eval_ingest.py prep-queries eval/raw/corpus.jsonl --by-cluster
```

产出 `eval/raw/query_inputs/<簇>.jsonl`,每行只有 `id + source_text + nodes`。
**出题必须用这些文件,不要把 corpus.jsonl 直接喂 mimo**——出题器看到 overview 会照抄措辞,
出出来的是作弊题,向量路必然满分,评测就假了。

### ④ 出题(eval_gen 驱动 mimo)

```bash
.venv/bin/python scripts/eval_gen.py queries
```

读 `eval/raw/query_inputs/` 下每簇文件、按每批 ≤5 条记忆分组调用,逐行校验(expect 越界、
negative 带 expect 均拒收),坏行进 rejects.log,同样支持重跑补缺。
手动备选:system = `prompts/gen_queries_system.txt`,user = 单个 `query_inputs/<簇>.jsonl`
的内容,输出追加到 `eval/raw/queries_raw.jsonl`。

### ⑤ 清库 + 落库(联网,一次性嵌入额度)

```bash
# 清空真库(现有碎片会移到 ~/.memory_system/backup_<时间戳>/,不删)
.venv/bin/python scripts/eval_ingest.py reset

# 落库:校验→写碎片→铺时间戳→rebuild(DashScope 全量嵌入,190 条约一两分钟)→改写夹具
.venv/bin/python scripts/eval_ingest.py ingest eval/raw/corpus.jsonl eval/raw/queries_raw.jsonl
```

时间戳由 ingest 均匀铺到过去 365 天(首条最旧),衰减参数因此可测;同输入同 `--anchor` 可复现。

### ⑥ 评测

```bash
.venv/bin/python scripts/eval_recall.py                      # 总表 + 按 mode/type 分组
.venv/bin/python scripts/eval_recall.py --verbose            # miss 的题打出实际 top-k,调试用
.venv/bin/python scripts/eval_recall.py --out /tmp/a.json    # 留档
.venv/bin/python scripts/eval_recall.py --param w_activation=0.5 --out /tmp/b.json   # A/B
```

### ⑦ 看召回(webui)

```bash
.venv/bin/python -m memory_system serve
```

浏览器进「召回」屏:选 mode、输 query,左栏看结构化槽位(主/同源/联想、分数、衰减乘子),
右栏点「重构」看自然语言回忆。默认不刷时钟,评测态安全。

## 注意

- **query 向量要联网**:episode/concept(带 context)评测每题嵌一次,走 DashScope 额度;
  detail/concept(不带 context)不联网。
- **重构要过 chat provider**:webui 右栏的重构走当前配置的 agent(claude_cli 不烧 key)。
- `last_accessed_at` 是运行态,`index rebuild` 会把时钟重置回 activated_at——跨 rebuild 对比
  A/B 时这是特性不是 bug(每轮起点一致),但别在两轮之间手动刷时钟。
- 评测完想恢复真实使用:再跑一次 `reset`(合成库进备份),之后正常蒸馏积累即可。
