# memory_system 架构

> 给后续实例建立整体认知用。读这一份，就能知道:系统是什么、数据怎么流、每层归谁管、
> 关键接口在哪、哪里有坑。细到接口层(标了 `文件:函数`,可直接跳)。
> 历史施工过程不放这里,统一归档到 `history_notes/`。
>
> 最近更新:2026-07-07(S6 Phase 2:injected_log 去重/冷却、别名文中锚定、开场火花槽、
> recall 专用 provider 通道,见 §5.10/§6)。状态以 `HANDOFF_NOTES.md` 为准,
> 概念正典见 `../project/idea_v2.md`。

---

## 1. 一句话

把 Claude Code 的对话记录(`~/.claude/projects/**/*.jsonl`)经人工审核的流水线,蒸馏成
**结构化长期记忆**:每条记忆是一份 Markdown「碎片」正本,SQLite 只是它的可重建索引(向量
检索 + 全文 + 概念图)。零依赖、零构建、纯本地、key 不落盘。

技术底座:Python ≥3.11 标准库 + 唯一依赖 `sqlite-vec`(vec0 向量表);HTTP 用 `http.server`;
前端是原生 JS 静态资源(无打包)。包名 `memory_system`,CLI 入口 `memory-system`。

---

## 2. 三条铁律(load-bearing,改任何东西先过这三关)

1. **碎片是正本,SQLite 是可重建索引。**
   真相只在 `fragments/` 下的 `.md`。DB(含膜、向量、FTS)删了能用 `index rebuild` 从碎片
   无损还原。任何「只在 DB、碎片没有」的状态都是 bug——除了纯操作态书签(见铁律 3 例外)。
   引擎:`archive.py`(写)/ `index.py`(重建)。

2. **uuid / 向量永不上台面。**
   message-uuid 只允许存在于操作态/工作态:段级书签表(`processed_segments`)和
   `staging/chunks|episodes` 的 `covered_uuids`。它不进碎片正本、不进 active/read API 返回、不上 UI;
   送前端前一律经 `server.py:_ui_*` 剥 `covered_uuids`。向量同理(`views.py` 的 dataclass 本就无此字段)。

3. **key 不进代码/仓库/前端;运行时只从环境读。**
   真 key 可以由仓库外的数据主目录 `.env` 注入,但代码只读 `os.environ`,HTTP 只回掩码
   (`registry.mask_key`),绝不把明文 key 传给前端。换 embedding 模型 → meta 锁拒写旧维度,
   必须从碎片全量重嵌。

补充不变量:**段/五件套是「工作态」,不是正本**(`staging/` 下的 JSON,可丢弃,删了不影响记忆)。
只有 S5 confirm 才把工作态固化成 active 碎片。

---

## 3. 数据流水线(S1–S5)

```
Claude Code *.jsonl
   │  transcript.discover / describe       发现层:廉价 stat + 首行嗅探,不全量解析
   ▼
preprocess.clean → CleanedTranscript        清洗成 [我]/[Claude] 回合 Turn[]
   │   (结果进 preview_cache,键=路径+mtime,可丢弃派生物)
   │  render_for_chunk
   ▼
[S3] chunk.run_chunk  (Prompt 1 + agent)     切成叙事弧线闭合的「段」,回合区间→covered_uuids
   │   落 segments_store →  staging/chunks/<session>.json   ← 工作态
   │   人工在「切段屏」审/编辑/手动建段(前端 chunk.js)
   ▼
[S4] extract.extract_segments (Prompt 2 + agent)  逐段独立提取「五件套」,按块回滚
   │   落 staging_store →  staging/episodes/<session>.json  ← 工作态
   │   人工在「蒸馏/审核屏」审/编辑五件套(前端 triage.js)
   ▼
[S5] archive.confirm_episode                 审核确认:固化成正本 + 增量入库
   │
   ├─▶ fragments/episodes/<public_id>.md     episode 碎片(正本)
   ├─▶ fragments/nodes/<label>.md            node 碎片(正本,三选一:命中/别名/新建)
   └─▶ memory.db                             增量插 episode + 膜 + 向量;FTS 由触发器自动同步
                                  ▲
                                  │  index.rebuild —— 删库后从碎片全量重建(向量/FTS/膜)
   查看侧 views.py(只读) ────────┘   →  前端 view.js galaxy 力导向图
```

**五件套**(extract 的产物,契约严校,见 `extract.py:_parse_extraction`):
`overview` / `summary` / `highlights`(0–3 条逐字) / `nodes`(概念,三选一) / `salience_tier`(1–3)。

**按块回滚**:`extract_segments` 逐段提取,坏段不拖好段——N 段成、M 段坏 → 成的进 staging、
坏的进 retry 列表,不整批失败。

**并发 + 逐条落盘**(2026-06-26):批量提取走线程池(`max_workers`,server 取 4),**慢 I/O(逐段
LLM 调用)并发,结果消费/落盘回主线程串行**(`as_completed` 在调用线程逐个 yield)——故落盘回调
无需锁、不竞争 staging 文件。每段一完成立即经 `on_staged`/`on_failed` 回调写盘,**批量提取中途
退出已完成的段不丢**(根治旧的「批末一次性落盘」)。provider 都无状态(claude_cli 起独立子进程、
openai_compat urllib 单发),并发安全。`max_workers=1`(默认)= 纯顺序,供 CLI / 行为脚本测试沿用。

**回合(turn)是统一单位**:切块 agent 输出回合号(非行号),`covered_uuids` 由回合区间直接回映
(`chunk.py:_covered_uuids`),杜绝多行消息的计数错位。

---

## 4. 存储层(各自归谁、能不能删)

| 存储 | 路径 | 角色 | 可删? | 归属模块 |
|---|---|---|---|---|
| **碎片正本** | `~/.memory_system/fragments/{episodes,nodes}/*.md` | 记忆真相 | **否** | `fragments.py` |
| **SQLite 索引** | `~/.memory_system/memory.db` | 可重建索引(向量/FTS/膜/书签) | 是(可 rebuild) | `db/`, `index.py` |
| 切段工作态 | `staging/chunks/<session>.json` | S3→S4 中间态 | 是 | `segments_store.py` |
| 提取工作态 | `staging/episodes/<session>.json` | S4→S5 中间态 | 是 | `staging_store.py` |
| 预览缓存 | `cache/jsonl_preview/*` | 清洗结果派生物(键=路径+mtime) | 是 | `preview_cache.py` |
| 导入的 jsonl | `imports/*.jsonl` | 前端上传的对话(transcripts_root 之外的第二发现根) | 是 | `server._api_import` |
| 自定义 provider | `custom_providers.json` | 控制台加的端点目录 | 是(配置) | `agent/registry.py` |
| 环境/密钥 | `.env` | 占位/真 key(只读进 os.environ) | —— | `env.py` |

主目录默认 `~/.memory_system`,`MEMORY_SYSTEM_HOME` 覆盖。所有路径布局集中在
`config.py:Config` 的 `@property`(`db_path` / `fragments_dir` / `chunks_dir` / …);`all_dirs()` 列全集供 init 建目录。

---

## 5. 模块地图(按层,带关键接口)

### 5.1 配置与环境
- **`config.py`** — 单一配置源。
  `Config`(home/embedding/agent/transcripts_root + 目录布局 property);
  `EmbeddingConfig` / `AgentConfig`(都 frozen);`load_config()`(确定 home → 灌 `.env` → 读 env 配置 → 注入 custom provider 映射);
  `AgentConfig.provider_for(role)`(chunk/extract 的有效 provider:专用 > 默认)。
- **`env.py`** — 零依赖 `.env` 读写。`parse_env` / `load_dotenv(path, override=)`(已 export 的环境优先,除非 override);`update_dotenv(path, updates)`(写回 .env:改值/追加,保留注释空行,并同步 `os.environ`——控制台改 provider/model、加自定义 provider 占位 key 都走它)。

### 5.2 transcript 接入与清洗
- **`transcript.py`** — 发现层。`discover(root, *, pattern="*/*.jsonl")` / `describe(path)` → `TranscriptInfo`(只 stat+嗅探)。Claude 是 `<encoded-cwd>/*.jsonl`;导入目录是扁平的 `*.jsonl`,两根都扫。
- **`preprocess.py`** — 清洗。`clean(path)` → `CleanedTranscript`(`Turn[]`);`render_for_chunk`(喂切块,带回合号)、`render_source_text(ct, start, end)`(喂提取,逐字原文)。按实测噪声分类剥离 system/tool/thinking 等。
- **`preview_cache.py`** — `get(cache_dir, path, mtime=)` 取/建缓存;`sweep_stale` 清旧 mtime 文件(已接在 `get` 后)。

### 5.3 蒸馏引擎(调 agent)
- **`chunk.py`**(S3,Prompt 1)— `run_chunk(...)` → 段;`manual_segments(...)`(不走 agent,始终可用);`validate_segments`;超大输入抛 `OversizedError`(绝不静默截断)。
- **`extract.py`**(S4,Prompt 2)— `extract_segments(..., max_workers=1, on_staged=, on_failed=)` → `ExtractBatch`(按块回滚 + 并发/逐条落盘:work 并发、consume 主线程串行);`run_extract`(单段);`existing_nodes(nodes_dir)`(读 active node 喂三选一);五件套严校 `_parse_extraction`。
- Prompt 正本在 `prompts/chunk_system.txt` / `extract_system.txt`。

### 5.4 工作态持久化(可丢弃 JSON)
- **`locks.py`** — 进程内锁注册表 `lock_for(key)`(按 key 的 RLock)。两个 store 的
  「load→改→_write」按 `chunks:|staging:<session_id>` 互斥(server 多线程防丢更新);
  provider 目录/cfg 热改用 `registry.CUSTOM_LOCK`。只防线程不防跨进程(本地单进程,够用)。
- **`segments_store.py`** — 切段态读写。`load` / `save_full` / `record_agent_run` / `merge` / `split` / `set_boundary` / `delete`;`recompute_uuids`(段边界变 → 重算覆盖集)。
- **`staging_store.py`** — 提取态读写。`load` / `upsert_episode`(成功即清该段 retry) / `get_episode` / `edit_episode`(白名单字段) / `remove_episode`(干净删,不留痕) / `reject_episode`(留痕) / `append_retry`(记失败,同段只留最新) / `clear_retry`(按 seg_id 关闭失败标记,幂等,不动 episodes)。

### 5.5 入库闭环 与 正本
- **`archive.py`**(S5)— `confirm_episode(...)`(staging→碎片+DB,**落地顺序硬约束**:所有可失败动作[embedding/向量/约束]在事务内 commit 之后,才原子写碎片);`reject_episode` / `archive_episode`(active→archived 软降级);`_plan_nodes`(node 三选一只在内存规划)。失败时**不写任何碎片、staging 原封不动**。
  **真删(误入库)**:`delete_episode(cfg, public_id)` / `delete_node(cfg, label)` → `DeleteReport`。删除**落地顺序与 confirm 相反且这是对的**——碎片正本先删、再删 DB,中途失败最坏剩悬空 DB 行(`doctor` 对账 + `index rebuild` 丢弃),**绝不被 rebuild 复活**;`episode_vectors` 是 vec0 不吃 FK,显式删,膜/FTS 由 FK 级联/触发器随 episode 行删。删 node 还必须**从所有引用它的 episode 碎片摘掉该 label 并回写**(否则 rebuild 用 `ensure_node` 复活成桩);删 episode 后变孤儿的 node **保留**,在 `DeleteReport.orphaned_nodes` 点名(由用户再决定是否 `delete node`)。
- **`editor.py`** — 编辑写回。`edit_episode(cfg, public_id, fields, emb_provider)` → `EditReport`,改 active/archived 碎片的**正文四件**(`EDITABLE = overview/summary/highlights/salience_tier`;传白名单外的键即报 `EditError`,明确 source_text/nodes 不可改)。**落地顺序同 confirm**:重嵌/向量/DB 在事务内 commit 成功后才回写碎片。**重嵌只在 overview 真变时做**(它是唯一进向量的字段),改 vec0 = 删后插 + 刷 last_embedded_at;summary/highlights/tier 改了不联网,source_text 不变故 FTS 由 `episodes_au` 触发器重灌同内容、无副作用。no-op(值没变)直接返回不写盘。
- **`fragments.py`** — 正本读写。`serialize/parse_episode`、`serialize/parse_node`、`write/read_episode`、`load_all_episodes`;`Episode` / `Node` dataclass。格式 = Markdown frontmatter + 分节,`source_text` 永远是最后一节(逐字读到 EOF,绕开 markdown 误判)。零依赖手写解析,保 round-trip。
- **`index.py`** — `rebuild(cfg, provider, lock_meta=)` 从碎片全量重建;`assert_embeddable`(写向量前校验 model/dim 与 meta 锁一致,不符拒写、不补零不截断);`insert_episode` / `ensure_node`。
- **`processed.py`** — 操作态书签(uuid 只在此)。`segment_hash`(排序 uuid 集的 sha1,顺序无关稳定身份);`mark_segment` / `is_processed` / `processed_uuids` / `get_watermark`。**不参与碎片重建**,删库即丢(可接受代价)。

### 5.6 查看侧(只读)
- **`views.py`** — `list_memories(cfg, include_archived=)` / `read_memory(cfg, public_id)`(带 source_text) / `read_node_detail(cfg, label)`。
  **node↔node 边 = 共享 episode 的共现**(idea_v2 §17/§96 正典):同一 episode 的多个 node 两两连边,`via` 给共享情景。当前实时算 O(E·K²),`views.py:85` 有 NOTICE,物化 edges 表是「等数据量证明价值再建」的可选缓存。

### 5.7 provider 子系统(2026-06-25 已收编)
- **`agent/registry.py`** — **所有 provider 知识的单一来源**(过去散在 config/工厂/server 三处)。
  `BUILTINS`(内置目录:id/显示名/kind/默认 base_url/key_env)、`BUILTIN_IDS`、`builtin(id)`、`is_builtin`;
  `load_custom` / `save_custom` / `custom_map`(`custom_providers.json` 的 list 源 + 派生 map);
  `all_provider_ids(cfg)` / `providers_info(cfg)` / `agent_key_status(cfg)`(各 provider key 状态);
  `mask_key(env_var)`、`PLACEHOLDER_KEY`(占位 key 常量,唯一定义处)。
- **`agent/__init__.py`** — 工厂 `get_chat_provider(cfg)`(按 `registry.builtin().kind` 分派)+ `probe_provider(cfg, id, custom_map)`(建 provider→`available()` 的探活,不发真实请求,失败折成 `(False, 原因)`)+ `extract_json`(剥围栏、定位平衡 `{}`)。
- **`agent/base.py`** — `ChatProvider` 接口(`complete(system,user,*,model,timeout)→ChatResult`、`available()→(bool,why)`)、`ChatError/ChatTimeout`。
- **`agent/{claude_cli,openai_compat,fake}.py`** — 三个实现。claude_cli 走本机 `claude -p`(复用订阅、不烧 key);openai_compat 走 urllib(deepseek/qwen/自定义共用,base_url+key_env 注入);fake 离线确定性。
- **`embedding/`** — 对称的另一族:`get_provider(cfg.embedding)` 工厂 + `probe(cfg)→(ok, detail, dim)`(嵌单词探活,不抛异常)+ `dashscope`(text-embedding-v4,1024 维)/ `fake`。

> provider 依赖方向(避免循环):`registry` 顶层只 import 标准库,对 `Config` 走鸭子类型;
> `config.load_config` 与工厂内部对 `registry` 都做**函数内 import**。

### 5.8 HTTP 服务(前端后端)
- **`server.py`** — 纯标准库 `ThreadingHTTPServer`,只绑 `127.0.0.1`。**只做 HTTP 编排**:路由 + 薄 handler,
  非 HTTP 逻辑已下沉(`.env` 写回→`env.update_dotenv`、探活→`embedding.probe`/`agent.probe_provider`、形状裁剪→`ui_shape`)。
  `make_handler(cfg)` 闭包出 `Handler`;路由:GET 用 if 链、**POST 用 dict 派发**(`do_POST` 的 `routes`)、DELETE/PUT 各一条。
  路径安全:`_confine(raw)`(限制在 transcripts_root 内,堵任意文件读)、`_valid_session_id`。
- **`ui_shape.py`** — 铁律 2 的单一执行点:`ui_segment/ui_episode/ui_staging/ui_doc`,送前端前从工作态 JSON 剥 `covered_uuids`。

### 5.9 CLI
- **`cli.py`** — `prog="memory-system"`。命令:`init` / `migrate {status,up,down}` / `doctor` / `scan` / `preview` / `serve` / `chunk` / `extract` / `confirm` / `reject` / `archive` / `delete {episode,node}` / `index rebuild` / `embed`。每个 `cmd_*(cfg, args)→int`。`delete` 是真删(碎片 + DB 永久移除,区别于 `archive` 软降级)。`doctor` 含碎片↔DB 双向一致性对账(孤儿碎片 / 悬空索引 / 坏碎片)。
- **`diagnose.py`** — `diagnose_claude_code(cfg)` 实测平台事实(jsonl 形态/uuid/role),落报告到 `diagnostics/`。

### 5.10 检索层(S6,`recall/`,Phase 1+2 完工 2026-07-07)

CLI 入口 `recall {detail,episode,concept}` + `opening {rebuild,show}`。结构化输出契约正本 =
`../project/s6_build_plan.md` §5;裁定与参数依据在两份施工书(Phase 1 `s6_build_plan.md` /
Phase 2 `s6_phase2_build.md`),此处只留架构地图。

- **`detail.py` — 逐字层**。FTS5 trigram grep + snippet 开窗(`--since/--until` 时间过滤、
  `--raw` 整条原文)。无 embedding/无 LLM,**豁免衰减与去重**(确切措辞的查找不该被
  「最近想没想起」干扰);命中刷时钟。中文查询 ≥3 字(trigram 实测边界)。
- **`episode.py` — 语义层,全系统唯一的「搜」**。管线:向量+FTS 双路 → RRF(只用名次)→
  active 硬过滤 + session 去重 → 衰减乘子 × 跨 session 冷却 → 三槽填装(主/同源/联想)→
  别名锚定 → 收尾落日志刷时钟。
  - **session 去重 / 跨 session 冷却(Phase 2,仅本路)**:`recall_episode(..., session_key=None)`。
    session_key 非空时,同 session 已注入过的 public_id 从**三槽全部候选硬排除**(已在对方
    上下文里);其他 session 在 `cooldown_hours` 窗口内注入过的,final 分乘 `cooldown_factor`
    (默认 0.8,**只降序不排除**——回忆线索永远响应)。收尾 `touch=True` 时把返回的三槽全部
    条目写 `injected_log`(与时钟刷新同一事务;m004,见 §6)。**无 session_key = 全关**
    (= Phase 1 行为;CLI 默认不带、eval 夹具天然豁免);计划中的 MCP server 会成为第一个
    真实消费方(每连接生成,见 `../project/mcp_build_plan.md`,当前仓库尚未落地)。「注入过」管去重、「被回忆」管衰减,
    是两件事——联想槽照旧不刷时钟。
  - **别名文中锚定(Phase 2)**:三槽每条 episode,所挂 node 的 alias 在其**库内** source_text
    字面出现且规范 label 未出现 → 附 `alias_bridges`(「文中「<alias>」= 概念 <label>」,
    无桥接省略该键),随槽位进重构输入当词义锚点。判据 Python 子串,零索引零开关。
- **`concept.py` — 调档层,「取」不是「搜」**。label→alias 精确解析(miss 回子串建议)→
  膜 join **全量**取 active(「全部记忆」语义,故豁免去重);不取 source_text;只刷 node 时钟。
  经 alias 进来附入口桥接行 `alias_bridge`——与 episode 的文中锚定是两回事(入口解析 vs
  文中锚定,文案引号刻意不同)。
- **`decay.py` — 惰性衰减**。活跃度读时现算 `0.5^(闲置天数/半衰期)`,半衰期按 salience_tier
  14/90/365 天;不落库不 commit(谁调用谁 commit),改配置全库即时生效。时钟规则四条:
  detail 刷命中、episode 刷 top-1+同源(联想不刷)、concept 只刷 node、开场全不刷。
- **`reconstruct.py` — 重构 agent,检索层唯一 LLM 出口**。**候选集程序定死、调用前写可重放
  日志**,LLM 只做表达;失败抛 ChatError,CLI 降级结构化输出(退出码 3)。**recall 专用
  provider 通道(Phase 2)的单一解析点**:`AgentConfig.recall_provider` 非空 →
  `dataclasses.replace(cfg.agent, provider=...)` 再 `get_chat_provider`(episode/concept/opening
  共用此点,一处接线全覆盖);key/掩码/custom provider 全复用 `agent/registry`,零复制;
  空串 = 跟随全局。
- **`opening.py` — 开场注入,窥视不回忆**。三槽:A 最新 1 条、B 压舱(tier≥2 活跃度最高
  1–2 条)、C 火花(Phase 2)——候选 = 全部 active − A/B 已选,权重
  `w = salience_tier × (1 − activation) + 0.05`(重要且沉睡),按 `p ∝ w^(1/T)` 温度采样;
  spark 开启保留 1 席、ballast 取剩余预算,`opening_max_items` 硬顶;`opening_spark=0` 回
  Phase 1。**全程不刷时钟**;函数收 `rng` 形参(verify 注入固定 seed,生产走系统熵)。
  原子写 `opening_cache/global.md`,dirty 标记接 confirm/archive/delete/edit 四点;
  SessionStart hook 只读缓存文件,接线在仓库外。

配套与红线:
- **配置**:`RecallConfig` 全走 `MEMORY_RECALL_*` 覆盖。半衰期和 Phase 2 新旋钮
  (`DEDUP_SESSION / COOLDOWN_HOURS / COOLDOWN_FACTOR / OPENING_SPARK / OPENING_SPARK_TEMP`)
  有坏值回落;早期的 topk/rrf/槽宽/开窗/开场预算仍是直接 `int/float` 解析,坏值会让
  `load_config()` 报错。`AgentConfig.recall_model`(默认 sonnet——候选集已定死、重构只做表达)
  + `recall_provider`(env `MEMORY_AGENT_RECALL_PROVIDER`)。
- **逃生口与控制台**:`--json` = 结构化契约(不调 LLM)、`--raw` = 人类可读渲染(调试);
  `/api/agent/config` recall 节管 model + provider(GET 如实回 override 原值,POST 以
  `"provider" in body` 区分「未传」与「传空串清 override」;删 custom provider 连带清悬空
  recall_provider);`/api/recall` + 前端召回屏走同一引擎(touch 默认 false)。
- **红线**:所有对外输出手工挑字段,只 public_id / node label,无 uuid/向量/DB 整数 id;
  所有排序带 tie-break(public_id/created_at),同库同 query 可重放。
- **评测**:`eval/queries.jsonl` + `scripts/eval_recall.py`(hit@k、`--param` A/B、
  `touch=False` 只读不污染时钟);合成语料基建见 `../project/eval_build_plan.md`。
- **性能标记(刻意延后)**:联想槽与 concept context 排序在应用层 Python 算 L2(`_l2`),
  库大了下沉 SQL/vec0。

---

## 6. DB schema(`db/migrations/`,迁移器记账)

迁移器 `db/migrate.py` 用 `applied_versions()` 读**实际行集合**判缺口(非 MAX 推断)。
连接 `db/connection.py`:`busy_timeout=5000` + WAL + 加载 vec0。

| 版本 | 表 | 要点 |
|---|---|---|
| m001 meta | `meta(key,value)` | 锁 embedding model/dim;写向量前校验,不符拒写。值由 `init` 用当前 config 写,迁移不硬编码维度。 |
| m002 core | `episodes` | 五件套 + 状态机 `status∈{staging,active,rejected,archived}`、`salience_tier 1–3`、粗溯源 `source_session_id/path`、`fragment_path UNIQUE`、`embedding_model/dim`。 |
| | `nodes` / `node_aliases` / `episode_nodes` | 概念图;膜表 `episode_nodes` 是 episode↔node 多对多,**FK CASCADE**。 |
| | `episode_vectors` | vec0 虚表,`FLOAT[N]` 维度建表时从 `config.embedding.dim` 定死(故 m002.up 调 `load_config`)。 |
| | `episode_fts` | FTS5 trigram,外部内容表 `content='episodes'`,3 个触发器(ai/ad/au)自动同步 `source_text`。 |
| m003 processed | `processed_segments` / `session_watermark` | 段级已处理书签 + 会话水位。**纯操作态,不参与 rebuild**,uuid 只圈在此。 |
| m004 injected_log | `injected_log` | episode 注入日志(session_key/public_id/tool/hit_at + 索引 `(session_key,public_id)`、`(public_id,hit_at)`)。**身份是 public_id**(整数 id 跨 rebuild 会重分配即腐坏),**`index rebuild` 不清这张表**(记「注入过什么」,与索引重建无关)。`tool` 列现恒 'episode',留给将来 MCP 填真实 tool 名。 |

---

## 7. HTTP API 速查

读(GET):`/api/transcripts[?q=]`(q=对原始 jsonl grep,大小写不敏感,不清洗;含导入目录;返回带 `imported` 标记)、`/api/transcript?path=`、`/api/segments?path=`、`/api/staging?{session_id|path}`、
`/api/staging/all`、`/api/memories[?include_archived=1]`、`/api/memory?public_id=`、`/api/node?label=`、
`/api/agent/providers`、`/api/agent/config`、`/api/prompts`。

写(POST):`/api/select`、`/api/chunk`、`/api/segments`(存段)、`/api/segments/delete`、`/api/extract`、
`/api/confirm`、`/api/reject`、`/api/archive`、`/api/staging/edit`、`/api/staging/delete`、
`/api/staging/retry/clear`(关闭/忽略提取失败卡:按 seg_id 清 retry)、
`/api/agent/config`(改 provider/model,写 `.env`)、`/api/agent/test`、`/api/embedding/test`、
`/api/import`(`{filename, content}` 上传一份 jsonl 落 `imports/`——浏览器文件选择器只给内容不给真实路径,故走上传)、
`/api/prompts`(过程 prompt 白名单写回)、`/api/recall`(三路检索,可选重构,touch 默认 false)。

provider 增改删:`POST/PUT/DELETE /api/agent/providers`。

编辑(POST):`/api/memory/edit {public_id, fields}`——正文四件写回(改 overview 回 `reembedded:true`),回 `changed`/`reembedded` + 更新后的整条 `memory`。越权字段(source_text/nodes)400。

删(DELETE):`/api/memory?public_id=`(真删 episode,回 `orphaned_nodes`)、`/api/node?label=`(真删 node,回 `dereferenced_episodes`)。区别于 `POST /api/archive`(软降级)。删不存在的回 404。

> 详细形状契约见 `../project/frontend_plan.md` §7/§8。

---

## 8. 前端(`web/`,零构建原生静态资源)

`index.html` 定脚本顺序,各 JS 经 `<script>` 拼接共享**隐式全局作用域**(无 ES module)。

| 文件 | 职责 |
|---|---|
| `state.js` | 全局状态收进**单一 `ST = {}` 命名空间**(P2 已收口,2026-06-26)、localStorage 游标、通用工具(`esc`/`toast`/`clone`)、`PALETTE`/`LS_KEY` 常量。8 个 JS 仍隐式全局共享,但可变态都挂 `ST.*`(`ST.cur`/`ST.lock`/`ST.tris`…),无裸全局碰撞 |
| `api.js` | `postJSON`、按 role 选默认 provider、`once(key,fn,btn)` 在途锁 |
| `transcripts.js` | transcript 列表、候选篮子、阶段切换、`beginEdit`/`markDirty`;`loadList(q)` grep 搜索、`sortedList` 模式×方向(time/touched × desc/asc)、`importFiles` 上传导入 |
| `chunk.js` | 切段屏:`runChunk`、段操作、`showAlert`/`renderAlerts` |
| `triage.js` | 蒸馏/审核屏:段预览、五件套编辑、批量 confirm/reject/delete |
| `view.js` | 四视图导航(写入/查看/召回/控制台,切换=显隐冻结不销毁)+ galaxy 力导向图。node/episode 详情面板带**真删**按钮(二次 confirm;删 node 提示「将从 N 条 episode 摘除」;删后 unfocus + 重拉 `/api/memories`)。episode 面板带**编辑**态(`showEpisodeEditor`):正文四件表单(overview/summary textarea、tier 下拉、highlights 动态行 ≤3),source_text/nodes 只读;存→`POST /api/memory/edit`→回读刷新 |
| `recall.js` | 召回屏:detail/episode/concept 三路检索、结构化槽位展示、episode/concept 可一键重构;touch 默认 false |
| `console.js` | 控制台:agent 配置/切换/保存、自定义 provider 增删、key 掩码、连接测试、过程 prompt 在线编辑 |
| `app.js` | 启动与事件绑定 |

四视图导航 = 显隐冻结(不销毁,保状态)。LLM 在途有处理锁防连点误触。

---

## 9. 验证

后端回归(用项目 `.venv`,否则可能因 `sqlite_vec` 缺失失败):
```bash
.venv/bin/python scripts/verify_s1.py …… verify_s5.py
.venv/bin/python scripts/verify_delete.py        # 删除层(真删 episode/node + 删后 rebuild 不复活)
.venv/bin/python scripts/verify_edit.py          # 编辑写回(正文四件 + 改 overview 重嵌 + 编辑落正本)
.venv/bin/python scripts/verify_s6.py            # S6 检索层(含 Phase 2:去重/冷却、alias_bridges、spark)
.venv/bin/python scripts/verify_web_api.py / verify_view_api.py / verify_provider_config.py
```
> **本机有 socks/http 代理坑**:跑起本地 `ThreadingHTTPServer` 的测试(web_api / view_api /
> provider_config)前必须 `export no_proxy=127.0.0.1,localhost` 并 `unset http_proxy https_proxy all_proxy`,
> 否则 urllib 把 localhost 也走代理 → HTTP 502。


前端:`node --check web/*.js` + NUL 字节检查(见 HANDOFF §验证)。

---

## 10. 当前已知风险与后续改进

历史施工记录已移到 `history_notes/`:
- `history_notes/S5_NOTES.md` — S5 写入侧细节、前端注意、旧工程债修复。
- `history_notes/S6_NOTES.md` — S6 Phase 1/2、召回评测、MCP 化前置。
- `history_notes/ENGINEERING_HISTORY.md` — 横跨 S5/S6 的工程整理时间线。

仍需记住的当前风险:
- **confirm/edit 的「DB 先 commit、碎片后写」崩溃窗口**:commit 与写碎片之间进程被杀时,
  可能出现 DB 新碎片旧或 DB 有碎片缺;`doctor` 对账与 `index rebuild` 负责收口。
- **前端局部性能债**:勾选 transcript、选择回合仍有全量重渲染;galaxy 常驻 rAF;当前规模下可接受。
- **node 共现边实时计算**:`views.py` 仍按 O(E·K²) 现算,数据量大后再物化 edges 缓存表。
- **配置解析不完全容错**:部分 `MEMORY_RECALL_*` 早期参数坏值会让 `load_config()` 报错,见 §5.10。
- **冷缓存与重建成本**:`/api/transcripts` 冷缓存首次会清洗大量 jsonl;`index rebuild` 用真 DashScope
  会全量联网重嵌 overview。

下一步候选:
- MCP recall tools(施工书 `../project/mcp_build_plan.md`;注册上线仍需先过召回达标门)。
- 概念图(nodes 膜)编辑:给 episode 增删 node、孤儿 episode 可见化/重指派。
- SessionStart hook 真接线(仓库外配置,读 `opening_cache/global.md`)。
