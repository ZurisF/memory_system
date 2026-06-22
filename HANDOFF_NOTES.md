# memory_system 接手笔记

> 目的:让后续实例快速看清当前真实状态、优先隐患、方案文件可信度。
> 最近整理:2026-06-21, Codex 审查后重排;同日两个 P1 已修(见下)。
> 2026-06-21 晚更新:上一实例留下的 S5 GUI 草稿已 checkpoint 并收束核心写入链路。

## 先读结论

- 项目没有完全失控:S1-S5 验证脚本在项目 `.venv` 下全绿(verify_s3 增门 7、verify_s5 增门 H)。
- **两个 P1 已修(2026-06-21)**:① confirm 失败残留 node 碎片 → 已改成 DB commit 成功后才原子落盘;
  ② agent 切块坏边界静默夹紧 → 已改成越界/逆序/非整数抛错走重试。详见下方两节(保留复现上下文,标 ✅ 已修)。
- **S5 富审核 GUI 已有 Phase 1 写入侧草稿并已收束核心路径**:`web/` 现在有「切段 / 待整理区」两屏、
  候选篮子、处理锁、提取总结、staging 五件套编辑、批量 confirm/reject。上一实例的大改已先提交为
  `2dd8d17 checkpoint: prior frontend triage draft`;随后又修了 session_id 审核契约、dirty 保护、nodes JSON
  编辑和 fake provider 默认提取。**未完成**:查看侧 demo、控制台、单开去噪对比、自动精炼 agent、galaxy。
- 完整前端施工书仍在:
  `~/Workspace/project/frontend_plan.md`——自包含、含精确 API 契约,下一棒只读它 + 文件清单即可写前端,
  不必通读全项目。zuris 现场设计了完整编辑器方案(写入流水线 + 查看 demo + 控制台),三处碰撞已拍:
  ① 控制台只提示去 `.env` 配 key、显示密文,key 不进前端;② galaxy 可视化推 Phase 2,先做轻量 read/edit;
  ③ node↔node 边推 Phase 2。Phase 1 用现有引擎 extract-first 路径(精炼=对 staging episode 就地编辑)。
- README 和 `phase1_build.md` 大体跟当前实现一致;`plan_v3.md` 和 `project/out/*` 是历史方案,有大量旧设定,不要按它们施工。

## 当前代码事实

- 包名/命令:`memory_system` / `memory-system`。
- 数据主目录默认 `~/.memory_system`,可由 `MEMORY_SYSTEM_HOME` 覆盖。
- 当前 GUI:`memory-system serve` 启标准库 `http.server` + 原生 HTML/JS,默认 `http://127.0.0.1:8765`。
- embedding 默认 DashScope `text-embedding-v4`,1024 维;fake provider 只用于离线测试。
- chat agent 层已有 `claude_cli` / OpenAI 兼容 / fake。切块默认 `sonnet`,提取默认 `opus`。
- S1-S5 引擎/API/CLI 基本完成:
  - S1:碎片正本、DB 重建、FTS、向量。
  - S2:transcript 发现、清洗预览、段级 processed 标记、空壳过滤。
  - S3:切块 agent + S3 GUI,段工作态落 `home/staging/chunks/<session>.json`。
  - S4:提取五件套,按块回滚,staging 工作态落 `home/staging/episodes/<session>.json`。
  - S5 第一段:confirm/reject/archive 的引擎/API/CLI,active episode 碎片 + 增量入库。
  - S5 GUI 写入侧:切段→待整理→extract→staging edit→confirm/reject 已接入,核心 API 有 headless 回归。
- 未完成:
  - S5 查看侧 demo(`GET /api/memories` 等尚未实现)与控制台(`GET /api/agent/config` 等尚未实现)。
  - S5 写入侧仍缺单开去噪对比/更精致的原文对比;当前是 staging episode 内联编辑 source_text。
  - S6 检索模块。
  - S7 recall 工具。
  - S8 开场注入。

## 本轮验证

用项目自带 `.venv` 跑过:

```bash
.venv/bin/python scripts/verify_s1.py
.venv/bin/python scripts/verify_s2.py
.venv/bin/python scripts/verify_s3.py
.venv/bin/python scripts/verify_s4.py
.venv/bin/python scripts/verify_s5.py
.venv/bin/python scripts/verify_web_api.py
```

结果全部 PASS。

`verify_web_api.py` 是本轮新增的 HTTP 级 smoke test:临时起 `ThreadingHTTPServer`,走 `/api/segments` →
`/api/extract(fake)` → 删除源 jsonl → 用 `session_id` 调 `/api/staging/edit|reject|confirm`。它证明已提取
staging episode 在源 transcript 清掉后仍可审核/入库。注意:在受限 sandbox 内直接绑定本地端口可能被拒,
需要允许临时绑定 `127.0.0.1` 后再跑。

注意:直接用系统 `python3 scripts/verify_s*.py` 会因包路径或 `sqlite_vec` 未安装失败。验证脚本跑法应写 `.venv/bin/python ...`,或先按 README `pip install -e .`。

`PYTHONPATH=.` 只影响单次命令,不会写入持久配置。本轮已清理项目代码目录的 `__pycache__` 与验证/复现留下的 `/private/var/.../T/memsys_*` 临时 home;`.venv` 内部包缓存保留,属于环境正常状态。

## 优先隐患

### P1 ✅ 已修(2026-06-21):confirm 失败会留下未确认 node 碎片

位置:`memory_system/archive.py`。**修法**:`_land_nodes`(写盘)拆成纯内存 `_plan_nodes`(返回膜 label
列表 + `{label: 待写 Node}`);`_upsert_node_db` 收 `planned` 参数,DB 同步用内存态而非读盘;`confirm_episode`
把 node 碎片 + episode 碎片的写盘**全部推迟到 DB commit 成功之后**。DB/向量阶段失败 → 不写任何碎片(含
node)、staging 原封不动,可干净重试。回归门 `verify_s5` 门 H:用「报 dim 过锁、embed 故意吐少一维」的
provider 触发 confirm DB 失败,断言 node 碎片/alias 不新增、staging 不动、DB 不增,修好 provider 后重试成功。
(旧代码上此门会失败——它是真回归测试。)

<details><summary>原隐患记录(已解决,留作上下文)</summary>

当前 `confirm_episode` 顺序:

1. `_land_nodes(...)` 先写/改 node 碎片与 alias。
2. 开 DB 事务,插 episode/膜/向量。
3. DB commit 后写 episode 碎片并清 staging。

问题:如果第 2 步失败(向量维度、sqlite-vec 插入、DB 约束、provider 异常等),DB 会 rollback,
staging 仍在,但第 1 步写出的 node 碎片/alias 已留在 `fragments/nodes/`。

Codex 用临时 home 复现过:confirm 故意失败后 `node_exists_after_failed_confirm=True`,
`staging_still_exists=True`。

影响:

- 违背"DB 失败不写碎片不动 staging"的入库闭环承诺。严格说 episode 碎片没写,但 node 碎片已污染。
- 失败/拒绝的候选概念会进入 `existing_nodes`,影响后续提取 agent 的三选一判断。
- 用户后来 reject 这条 staging 时,其 node 仍可能残留。

建议:

- confirm 前只在内存里规划 node 变更,先完成所有可失败动作(含 embedding),DB 与 episode 碎片成功后再原子写 node 碎片。
- 或将 node 写入改成可回滚的临时文件/rename 方案。
- 补测试:构造 confirm DB/向量失败,断言 node 碎片和 alias 不新增、staging 不动、episode 碎片不写。

</details>

### P1-A ✅ 已修(2026-06-21):agent 切块边界静默夹紧

位置:`memory_system/chunk.py`。**修法**:`_parse_turn_ref` 收紧——宽容 `回合5`/`T7`/`#5` 包裹但拒小数、
夹带字符(`5.5`/`abc`/`1-2` 都抛 `ValueError`);`_normalize_segment`(只走 agent 路径)对**越界**(回合
不在真实 `umap` 集合内)与**逆序**(`start>end`)抛 `ValueError`,走现有 `max_retries`/`ChunkFailed`,不再静默
交换/夹紧。`manual_segments` 的友好夹紧/交换是另一条路径,保留不动。回归门 `verify_s3` 门 7:`0-999`/`50-20`/
`10-3`/`5.5` 经 `run_chunk` 应抛 `ChunkFailed`;`_parse_turn_ref` 拒 5 种坏输入;`manual_segments` 仍夹紧。
(旧代码上 `0-999` 会被夹成 `1-20` 成功,此门会失败——真回归测试。)

### P2:fake provider / meta / vec 表锁有坑

位置:`memory_system/index.py`、`memory_system/db/migrations/m002_core.py`、`memory_system/cli.py`。

风险:

- `assert_embeddable` 在 meta 为空时按当前 provider 写 `embedding_model/embedding_dim`。真实 `~/.memory_system` 第一次跑 `index rebuild --provider fake` 可能把库锁成 `embedding_model=fake`,之后 DashScope 写入会被拒。
- `m002_core` 建 `episode_vectors` 时按当前 config dim 建表;若 provider dim 与建表 dim 不一致,可能不是清晰业务错误,而是在 sqlite-vec 插入时炸 `OperationalError`。

建议:

- README 把 `--provider fake` 明确限定为临时 `MEMORY_SYSTEM_HOME` 测试。
- 或 CLI 加显式 `--allow-fake-lock`,默认禁止 fake 在非临时 home 写 meta/向量。
- 在插 vec 前同时校验 provider dim、meta dim、vec 表 dim,错误信息必须可读。

### P2/P3:仍未处理的工程债

- 迁移器仍用 `MAX(version)` 判断当前版本:`memory_system/db/migrate.py`。坏状态 `001,003` 会误报 `002` 已应用。建议改 applied set。
- SQLite 连接没有 `busy_timeout`:`memory_system/db/connection.py`。GUI/CLI 并发写可能偶发 `database is locked`。
- `preview_cache.sweep_stale` 仍未接线,预览缓存会增长。
- `/api/transcripts` 冷缓存首次列表会 clean 全部 jsonl,大库下可能慢。
- `index rebuild` 对所有 episode overview 全量重嵌;真 DashScope 会联网、耗时、耗额度。README 已提示,但后续可加 `--skip-vectors` 或向量缓存。

## 已修旧项,不要重复修

这些是旧 handoff 里还残留过的隐患,当前代码已处理或不再适用:

- README 已补 `memory-system serve`、`scan`、`index rebuild`,并说明 GUI 目前只到 S3/S5 富审核待建。
- `m003_processed.py` 注释已改为 processed 表纯操作态,uuid 不进碎片,不参与 rebuild。
- `resume.py` 已删除。实测 `/resume` 在原 jsonl 追加,uuid 不跨文件;不再做跨文件 resume 检测。
- `/api/transcript`、`/api/select` 等 path 已经 `_confine()` 到 `cfg.transcripts_root` 内。
- node 文件名已加全 label 短 sha1 后缀,避免清洗/截断/大小写不敏感碰撞。
- frontmatter 写入已拒绝标量/列表项换行,避免 LLM 坏 label 写坏碎片。
- `index rebuild` 已改成先 parse/重嵌,全部成功才 `_clear` DB,不再网络失败后留下半清空库。
- P1-B 已补:保存段时禁重叠、允许空洞并返回 gaps。

## S5 第一段:归档闭环完工(2026-06-21)

里程碑「入库闭环」第一段(引擎/API/CLI,**富 GUI 留第二段**)。`python scripts/verify_s5.py`
全绿(8 门),`verify_s1`~`verify_s5` 全过,纯 fake 离线。zuris 拍板四决策:**增量插 DB / 先引擎
后前端 / P1-B 禁重叠允许空洞 / public_id=`ep_<8hex>`**。

- **P1-B 补完** `chunk.validate_segments(segments, turn_idxs)`:**禁重叠**(相交→硬错,回冲突区间)、
  **允许空洞**(未覆盖回合→回 gap 提示,不拒)。接进 `server._api_save_segments`(重叠 400,gaps 进
  成功响应)与 `cli.cmd_chunk`(手动/agent 落盘前都校验)。GPT 复查的 P1-B 已销。
  (P1-A「_normalize_segment 静默夹紧 agent 坏边界」**仍未处理**,zuris 本轮只点 P1-B;见下待办。)
- **staging 素材自洽**:transcript ~30 天会清,归档不能回读。`staging_store.upsert_episode` 增可选
  `created_at`(段首回合 timestamp),extract 两调用点(server/cli)传 `ct.turns[start-1].timestamp`。
  归档时作 episode 的 `created_at`(发生时间,§6「想想昨晚」靠它)。verify_s4 未受影响(参数可选)。
- **归档引擎** `memory_system/archive.py`(idea_v2 §9 两条退场通道):
  - `confirm_episode(cfg, session_id, stage_id, emb_provider) → public_id`:生成 `ep_<8hex>`(查重)→
    **node 三选一落地碎片**(new 建 / add_alias 并别名去重 / match_existing 复用,缺则补建不悬空)→
    组装 active Episode 写碎片 → **增量插 DB**(复用 `index.assert_embeddable/insert_episode`,嵌单条
    overview、插膜 + 向量,FTS 触发器自动)→ 清 staging。**DB 失败回滚且不写碎片不动 staging,可干净重试**
    (顺序:land nodes → DB 事务 commit → 写 episode 碎片 → remove staging)。
  - `reject_episode`:从 staging 移除、留痕 `rejected` 列表,不写碎片不进 DB。
  - `archive_episode(cfg, public_id)`:active 碎片 `status=archived`+`archived_at`,碎片 + DB 同步。
  - `confirm_all`:逐条确认该 session 全部 staging。
- **index.py 增量函数公开**:`_insert_episode`→`insert_episode`、`_ensure_node`→`ensure_node`(供 archive
  复用;`assert_embeddable` 本就公开)。`_insert_episodes`(复数,rebuild 内部)保持私有。
- **staging 编辑** `staging_store.edit_episode`:只改五件套 + 去噪后 source_text(白名单 `_EDITABLE`),
  `origin→edited`;`covered_uuids` 等工作态字段不可越权改。另加 `get_episode/remove_episode/reject_episode`。
- **API**(server.py):`POST /api/confirm|reject|archive|staging/edit`(confirm 用 `cfg.embedding` provider
  增量入库;沿用 `_confine` 路径校验、`_ui_staging` 剥 uuid)。do_POST 改路由表。
- **CLI**:`confirm <path> [--stage|--all] [--provider]`、`reject <path> --stage [--reason]`、
  `archive <public_id>`。
- **S5 GUI 写入侧现状(2026-06-21 晚)**:已有「切段 / 待整理区」两屏,待整理区扫
  `staging/chunks` + `staging/episodes` 聚类展示;可提取总结、内联编辑 staging 五件套/source_text、
  批量 confirm/reject。审核 POST 已支持 `{session_id, stage_id}`;源 jsonl 清掉后已提取 episode 仍可
  edit/reject/confirm。仍缺单开去噪对比、查看侧 demo、控制台。
- **关键不变量**:uuid 不进碎片(verify 读裸文件断言)、碎片是正本(删库 rebuild 无损还原,含 archived
  状态)、别名合并幂等(二次 add_alias 不长重复 node)。
  详细方案随 `phase1_build.md §S5`。

Codex 审查注(原文,记录):上面 S5 原交接是从 HEAD 原样恢复。当前代码实际在 DB 事务前先写 node 碎片;
若 DB/向量阶段失败,会残留 node/alias。这就是本文件 P1 第一项。
**更新(2026-06-21)**:该 P1 已修——node 碎片改为 DB commit 成功后才原子落盘,"DB 失败可干净重试"这句
现在对 node 碎片也成立了。见上方「P1 ✅ 已修」节。

## 方案文件可信度

### 可作为当前依据

- `README.md`:与当前代码最接近。需增强 fake provider 风险说明。
- `project/idea_v2.md`:当前概念正本。它的方向是人驱动入库、`salience_tier`、惰性衰减、无 cron、无可变 activation,与当前实现方向一致。
- `project/phase1_build.md`:当前施工脊梁,总体可信。注意其中 S0-S5 的通过门还用未勾选清单形式,不是实际状态;实际 S1-S5 已由脚本验证通过。S1 部分仍写 `episode_sources` / `processed_messages`,当前实现是 `processed_segments` 工作态且 uuid 不进碎片,这属于落地收敛后的差异。
- `project/prompts_extraction.md`:Prompt 2 基本可信;Prompt 1 的早期文字仍写"行号/L1",但真实打包 prompt 已改为回合制,见 `memory_system/prompts/chunk_system.txt`。
- `project/s3_chunking_plan.md`:S3 方案基本可信,但有早期 `line_map` / `L-range` 描述;当前实现已改成回合号。

### 历史参考,不要按它施工

- `project/plan_v3.md`:已经被 `idea_v2.md` 和 `phase1_build.md` 推翻/吸收。它仍写:
  - `claude-memory` 命令名和 `~/.claude-memory` 主目录;
  - 深夜 cron 批处理;
  - FastAPI/Jinja2/HTMX;
  - Ollama `bge-m3`;
  - `importance` / `activation` / decay cron;
  - `memories` 表、单 `node_id`、旧 `memory_sources` 等 schema。
  这些都与当前代码或后续施工方向冲突。建议在该文件顶部加"历史方案,勿施工"提示,或移入 `out/`。
- `project/out/plan_v1.md`、`project/out/plan_v2.md`、`project/out/idea.md`:历史草案,包含 daemon/socket/队列兜底/Ollama/FastAPI 等旧设计,只用于考古。

### 当前代码与方案的主要差异

- 命令名:当前是 `memory-system`,不是旧文档里的 `claude-memory`。
- 主目录:当前是 `~/.memory_system`,不是 `~/.claude-memory`。
- 前端:当前是标准库 `http.server` + 原生 HTML/JS,不是 FastAPI/HTMX。
- embedding:当前默认 DashScope `text-embedding-v4`,不是 Ollama `bge-m3`。
- 入库:当前是人驱动选段/切块/提取/确认,不是深夜 cron 自动批处理。
- 衰减:概念层已定为 `last_accessed_at + salience_tier` 惰性现算;不要恢复 `importance/activation` cron。
- 切块边界:当前打包 prompt 和代码按回合号;旧文档里的 L 行号/L-range 已过时。
- uuid:当前工作态内部保留 `covered_uuids`,但碎片和 UI 不暴露;processed 表是操作态书签,删库 rebuild 后可丢。

## 下一步建议顺序

1. ~~修 P1 confirm 失败污染 node 碎片~~ ✅ 已修(verify_s5 门 H)。
2. ~~修 P1-A agent 切块边界严校~~ ✅ 已修(verify_s3 门 7)。
3. **继续 S5 GUI Phase 1**:
   - 先手动浏览器验一下写入侧真实交互(候选篮子、dirty 保护、待整理内联编辑、批量 confirm/reject)。
   - 补查看侧 demo 所需 API:`/api/memories`、`/api/memory`、`/api/node`、`/api/memory/edit`、`/api/node/edit`。
   - 补控制台 API/UI:`/api/agent/config`、可选 `/api/agent/test`;key 只显示掩码,不进前端表单。
   - 单开去噪/原文对比可在查看侧前后择机做;自动精炼 agent 留 Phase 2。
4. (可顺手)收紧 fake provider 写锁、改迁移器 applied set、加 `busy_timeout`——见下方 P2/P3,非阻塞。
5. 再进入 S6 检索模块。

## 历史阶段摘要

- S3:可插拔 chat agent、回合制切块、段工作态、S3 三栏 GUI 已完成;前端浏览器交互未自动化测试。
- S4:五件套提取、严校、按块回滚、staging 工作态、`POST /api/extract`、CLI `extract` 已完成。
- S5 第一段:见上方专章;confirm/reject/archive 引擎/API/CLI 已完成。confirm 失败残留 node 的 P1 已于
  2026-06-21 修复(node 碎片改 DB 成功后原子落盘)。
- S5 GUI 写入侧:上一实例先做了过大的前端草稿并 checkpoint 为 `2dd8d17`;随后已收束为可验证的
  session_id 驱动审核链路。下一步 = 手动浏览器 QA + 查看侧 demo + 控制台。
