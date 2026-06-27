# memory_system 架构

> 给后续实例建立整体认知用。读这一份，就能知道:系统是什么、数据怎么流、每层归谁管、
> 关键接口在哪、哪里有坑。细到接口层(标了 `文件:函数`,可直接跳)。
> 末尾「修订意见」是当前已知的工程改进项。
>
> 最近更新:2026-06-25(provider 注册表落地后整理)。状态以 `HANDOFF_NOTES.md` 为准,
> 概念正典见 `project/idea_v2.md`。

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
- **`transcript.py`** — 发现层。`discover(root)` / `describe(path)` → `TranscriptInfo`(只 stat+嗅探)。
- **`preprocess.py`** — 清洗。`clean(path)` → `CleanedTranscript`(`Turn[]`);`render_for_chunk`(喂切块,带回合号)、`render_source_text(ct, start, end)`(喂提取,逐字原文)。按实测噪声分类剥离 system/tool/thinking 等。
- **`preview_cache.py`** — `get(cache_dir, path, mtime=)` 取/建缓存;`sweep_stale` 清旧 mtime 文件(已接在 `get` 后)。

### 5.3 蒸馏引擎(调 agent)
- **`chunk.py`**(S3,Prompt 1)— `run_chunk(...)` → 段;`manual_segments(...)`(不走 agent,始终可用);`validate_segments`;超大输入抛 `OversizedError`(绝不静默截断)。
- **`extract.py`**(S4,Prompt 2)— `extract_segments(...)` → `ExtractBatch`(按块回滚);`run_extract`(单段);`existing_nodes(nodes_dir)`(读 active node 喂三选一);五件套严校 `_parse_extraction`。
- Prompt 正本在 `prompts/chunk_system.txt` / `extract_system.txt`。

### 5.4 工作态持久化(可丢弃 JSON)
- **`segments_store.py`** — 切段态读写。`load` / `save_full` / `record_agent_run` / `merge` / `split` / `set_boundary` / `delete`;`recompute_uuids`(段边界变 → 重算覆盖集)。
- **`staging_store.py`** — 提取态读写。`load` / `upsert_episode` / `get_episode` / `edit_episode`(白名单字段) / `remove_episode`(干净删,不留痕) / `reject_episode`(留痕)。

### 5.5 入库闭环 与 正本
- **`archive.py`**(S5)— `confirm_episode(...)`(staging→碎片+DB,**落地顺序硬约束**:所有可失败动作[embedding/向量/约束]在事务内 commit 之后,才原子写碎片);`reject_episode` / `archive_episode`(active→archived);`_plan_nodes`(node 三选一只在内存规划)。失败时**不写任何碎片、staging 原封不动**。
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
- **`cli.py`** — `prog="memory-system"`。命令:`init` / `migrate {status,up,down}` / `doctor` / `scan` / `preview` / `serve` / `chunk` / `extract` / `confirm` / `reject` / `archive` / `index rebuild` / `embed`。每个 `cmd_*(cfg, args)→int`。
- **`diagnose.py`** — `diagnose_claude_code(cfg)` 实测平台事实(jsonl 形态/uuid/role),落报告到 `diagnostics/`。

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

---

## 7. HTTP API 速查

读(GET):`/api/transcripts`、`/api/transcript?path=`、`/api/segments?path=`、`/api/staging?{session_id|path}`、
`/api/staging/all`、`/api/memories[?include_archived=1]`、`/api/memory?public_id=`、`/api/node?label=`、
`/api/agent/providers`、`/api/agent/config`。

写(POST):`/api/select`、`/api/chunk`、`/api/segments`(存段)、`/api/segments/delete`、`/api/extract`、
`/api/confirm`、`/api/reject`、`/api/archive`、`/api/staging/edit`、`/api/staging/delete`、
`/api/agent/config`(改 provider/model,写 `.env`)、`/api/agent/test`、`/api/embedding/test`。

provider 增改删:`POST/PUT/DELETE /api/agent/providers`。

> 详细形状契约见 `project/frontend_plan.md` §7/§8。

---

## 8. 前端(`web/`,零构建原生静态资源)

`index.html` 定脚本顺序,各 JS 经 `<script>` 拼接共享**隐式全局作用域**(无 ES module)。

| 文件 | 职责 |
|---|---|
| `state.js` | 全局状态(~22 个模块级 let/const)、localStorage 游标、通用工具(`esc`/`toast`/`clone`)、`TPREVIEW` 缓存 |
| `api.js` | `postJSON`、按 role 选默认 provider、`once(key,fn,btn)` 在途锁 |
| `transcripts.js` | transcript 列表、候选篮子、阶段切换、`beginEdit`/`markDirty` |
| `chunk.js` | 切段屏:`runChunk`、段操作、`showAlert`/`renderAlerts` |
| `triage.js` | 蒸馏/审核屏:段预览、五件套编辑、批量 confirm/reject/delete |
| `view.js` | 三视图导航(写入/查看/控制台,切换=显隐冻结不销毁)+ galaxy 力导向图(只读) |
| `console.js` | 控制台:agent 配置/切换/保存、自定义 provider 增删、key 掩码、连接测试 |
| `app.js` | 启动与事件绑定 |

三视图导航 = 显隐冻结(不销毁,保状态)。LLM 在途有处理锁防连点误触。

---

## 9. 验证

后端回归(用项目 `.venv`,否则可能因 `sqlite_vec` 缺失失败):
```bash
.venv/bin/python scripts/verify_s1.py …… verify_s5.py
.venv/bin/python scripts/verify_web_api.py / verify_view_api.py / verify_provider_config.py
```
> **本机有 socks/http 代理坑**:跑起本地 `ThreadingHTTPServer` 的测试(web_api / view_api /
> provider_config)前必须 `export no_proxy=127.0.0.1,localhost` 并 `unset http_proxy https_proxy all_proxy`,
> 否则 urllib 把 localhost 也走代理 → HTTP 502。


前端:`node --check web/*.js` + NUL 字节检查(见 HANDOFF §验证)。

---

## 10. 修订意见(已知工程改进项,按优先级)

> 已完成:
> - **provider 注册表**(2026-06-25)——把散在 config/工厂/server 三处的 provider 知识收进
>   `agent/registry.py`,server.py 1092→970 行,占位 key 三处合一。
> - **P1 · server.py 抽薄**(2026-06-26)——三处非 HTTP 逻辑下沉:`_update_dotenv`→`env.update_dotenv`、
>   探活 `_api_embedding_test`/`_api_agent_test`→`embedding.probe`/`agent.probe_provider`、
>   `_ui_*`→新模块 `ui_shape.py`。server.py 970→887 行,只剩路由 + 薄 handler + 路径安全。
>   (注:原估 ~600 行偏乐观——剩下的 chunk/extract/confirm 等 handler 本身就是合法的 HTTP 编排,不再下压。)
>   verify 全套(s1–s5 + web/view/provider)重构前后均全绿。
>
> 下面是仍待做的。

**P2 · 前端全局变量(隐患最高,收益偏虚,建议单独一轮)。**
`state.js` ~22 个模块级可变全局靠隐式全局作用域跨 8 个文件共享,任何模块能改任何全局,无封装、
易命名碰撞。两条路:(a) 轻量——收进单个 `ST = {}` 命名空间对象(机械改,要扫全部 8 文件);
(b) 彻底——转 `<script type="module">` + import/export(更正但工作量大,只有 `node --check` 兜底,回归风险高)。

**P3 · 零散项。**
> 已修(2026-06-26):
> - `_api_add_provider` 的 hint 不再内联 `[this is your api key]` 字面量,改用 `{placeholder}`(=`registry.PLACEHOLDER_KEY`);
>   该字面量现在全代码只在 `registry.py:PLACEHOLDER_KEY` 一处定义。
> - 自定义 provider 的 `created_at` 不再写死 `None`(原 `# 略`),落真 UTC ISO 时间戳(与各 store 的 `_now()` 同写法)。
>
> 仍保留(刻意设计或已延后,非债):
> - `views.py` node↔node 边 O(E·K²) 实时计算,数据量大会慢;物化 edges 表是 idea_v2 明确「等数据量证明价值再建」的可选缓存,入口已留(`views.py:86` NOTICE),本轮不建。
> - `m002.up` 调 `load_config()` 取维度——迁移依赖运行时 config 是刻意设计(vec0 维度建表定死),已在其 docstring 说明,不算债。
> - `/api/transcripts` 冷缓存首次会 clean 全部 jsonl,大库下慢;`index rebuild` 全量重嵌真 DashScope 会联网耗额度。两者是固有成本,非可清理的债。
> - `cli.py` doctor 的「缓存 vs 真相一致性 / 孤儿碎片检查」仍是占位(`cli.py:124` TODO)——是真功能而非零散项,实现需扫 fragments↔DB 对账,留待单独一轮。

**未做但概念上待定(Phase 2,见 HANDOFF):** 编辑写回(`editor.py` + 改 overview 须重嵌)、
自动精炼 agent + diff 红绿块、段拖拽排序(语义冲突需先厘清)、多步 ctrl-z。
