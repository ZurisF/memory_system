# memory_system 交接隐患笔记

> 目的:给后续接手实例快速看懂当前项目真实状态、已知隐患。
> 日期:2026-06-20。

## 已修复(2026-06-20 续场)

- **#1 README 补 serve**:已加 `serve`/`index rebuild`/`scan` + S2 前端范围说明。
- **#3 m003 covered_uuids 冲突注释**:已改。结论锁定:uuid 绝不进碎片;processed 表纯
  操作态书签,**不参与碎片重建**,删库后为空、已处理标记不恢复(可接受代价)。
- **#8 resume 检测**:**整个 `resume.py` 已删**。原实现建立在「resume 复刻进新 jsonl、
  跨文件 uuid 重叠」的**错误前提**上;实测(见 `../project/session-jsonl-lifecycle.md`)
  证明 **resume 永远在原文件追加,uuid 永不跨文件**。`/api/transcript` 的 resume 字段、
  前端断点展示、`verify_s2` 的 resume 测试一并移除。「哪些回合没处理过」由已处理标记覆盖,
  不需要 resume 检测。
- **新增**:transcript 列表自动剔除清洗后 0 回合的空壳(`/clear` stub 等),返回
  `hidden_empty` 计数。

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
- **待接(S5 第二段)**:富审核 GUI —— 按父 jsonl 聚类卡片、五件套就地编辑、单开编辑 + 去噪(消费
  deletions)+ 原文对比、批量 confirm/reject、retry 列表挂载。本步未碰 `web/`。
- **关键不变量**:uuid 不进碎片(verify 读裸文件断言)、碎片是正本(删库 rebuild 无损还原,含 archived
  状态)、别名合并幂等(二次 add_alias 不长重复 node)。
  详细方案随 `phase1_build.md §S5`。

## S4 提取完工(2026-06-21)

引擎 + API + CLI 一把过,`python scripts/verify_s4.py` 全绿(13 门),真 `claude -p` **opus**
烟测通过(model=claude-opus-4-8,overview 索引卡/summary 弧线/highlights 正确空/salience=2,
单次 $0.036)。**前端富审核界面留给 S5**(本步不碰 `web/`)。

- **提取引擎** `memory_system/extract.py`:`run_extract(source_text, nodes, provider)` 调提取
  agent(默认 `opus`),`extract_json` → **五件套契约严校** → `ExtractResult`。严校挡:空
  overview/summary、highlights>3、salience 越界(非 1-3)、node action 非法、label/别名含换行
  (碎片不可写)——一律抛 `ValueError` 触发重试,**绝不静默夹紧**(对齐 GPT 复查 P1 精神)。
  屡败 `ExtractFailed`(带各次错误)。
- **按块回滚** `extract_segments(ct, segments, provider, nodes)`:逐段独立提取,坏段进
  `failed` 不中断好段 → `ExtractBatch{staged, failed}`。验过 5 切 3 成 2 坏 → 3 staging 2 retry。
- **existing_nodes**(三选一来源):读 **active** node 碎片(`fragments.load_all_nodes`)→
  `[{label, aliases}]`。S4 阶段多半为空 → 所有 node 判 new,符合预期;S5 归档后才积累。
- **source_text 一文两用** `preprocess.render_source_text(ct, start, end)`:回合区间渲染成纯对话
  (`[我]:/[Claude]:` + `---`,**无回合号脚手架**),**既喂提取 agent、又落 episode 正本**,
  同源逐字一致(索引/grep/highlights 命中的 == 提取依据的)。
- **deletions 不在 S4 物理删**(商讨锁定):source_text 存逐字全文,deletions 作建议元数据
  随段带进 staging;真正去噪交给 **S5 的单开编辑/对比界面**人工做。
- **staging 工作态** `memory_system/staging_store.py` → `home/staging/episodes/<session>.json`
  (**非正本、不进 DB/fragments**,守"碎片是正本")。`upsert_episode`(按 seg_id upsert,成功清
  该段 retry)、`append_retry`(同段只留最新)。`covered_uuids` 仅内部,**永不出服务端**;
  `source_text` 上 UI(S5 审核要看,内无 uuid)。
- **API**:`POST /api/extract {path, seg_ids?, provider?, model?}`(按块回滚、回 staged/failed
  计数 + staging 文档)、`GET /api/staging?path=`(剥 covered_uuids)。providers 端额外回
  `extract_model`。
- **CLI**:`memory-system extract <path> [--provider] [--model] [--seg s1,s3]`,段取自该 session
  的切块工作文件;落 staging,打印五件套表 + retry。
- **待接(S5)**:staging→active 审核/编辑/归档 GUI;单开编辑界面 + 手工去噪(消费 deletions)+
  对比;node 别名合并在确认时生效;确认时写 active 碎片。提取默认 opus 已钉。

详细方案随 `phase1_build.md §S4` 与 `prompts_extraction.md` Prompt 2。

## S3 切块完工(2026-06-20 续场三)

引擎 + 前端一把过,`python scripts/verify_s3.py` 全绿,真 `claude -p` sonnet 烟测通过。

- **可插拔 chat agent 层** `memory_system/agent/`:`claude_cli`(本机 `claude -p`,复用订阅不烧
  key,默认 sonnet)、`openai_compat`(DeepSeek/qwen,urllib)、`fake`(离线)。工厂 +
  `extract_json`(剥 ```json 围栏 / 抠平衡 {})。`AgentConfig` 进 config(`MEMORY_AGENT_*` 覆盖)。
- **回合制切块**(重要):render 不写显式 L 行号、多行消息令"逻辑行≠物理行",故切块**按回合编号**
  (`render_for_chunk` 出【回合 N】块,agent 输出回合号),回合是回映 uuid 的统一单位,1:1 直达。
  prompt 全文在 `memory_system/prompts/chunk_system.txt`(I/O 段已改回合制)。
- **段是工作态非正本** → `home/staging/chunks/<session>.json`(`segments_store`),不进 DB。
  每段 `origin: agent|manual|edited` + `covered_uuids`(仅内部,**永不出服务端**)。
- **错误管理**:超时/坏 JSON 重试 `max_retries`;屡败 `ChunkFailed` → 进 retry 列表 + UI 告警。
  超大(>12 万字符)`OversizedError`,要人工先粗分,绝不静默截断。
- **API**:`GET /api/agent/providers`、`POST /api/chunk`、`GET/POST /api/segments`。回存时
  uuid 一律服务端按回合区间重算(不信前端);送前端的段剥 `covered_uuids`。
- **前端三栏**:transcript | 回合(段色带覆盖)| 段面板(provider 下拉 + 运行 + 并/分/移边界 +
  编辑 tag·cut_reason + 标删 + 手动建段 + 保存)。**前端浏览器交互未自动化测试**,靠 API 契约
  + store 往返测覆盖通过门;接手者首次跑 `serve` 用浏览器过一眼。
- **待接**:claude 默认走会话模型,已 `--model sonnet` 钉死切块;S4 提取默认 `opus` 别名(无
  opus-4.6,当前最新 4.8)。`claude -p` 子进程必须 `stdin=DEVNULL`(否则干等 3s)。

详细方案:`../project/s3_chunking_plan.md`。

## 安全评估后修复(2026-06-20 续场二)

正本完整性 + 安全四条,趁碎片还没几份焊死:

- **#6 node 文件名碰撞 → 已修**:`_safe_node_filename` 加全 label 短 sha1 后缀,不同
  label(含 macOS 大小写不敏感)永不塌成同名静默覆盖正本。
- **#7 server 任意文件读 → 已修**:`/api/transcript`、`/api/select` 的 path 经 realpath
  校验必须落在 `transcripts_root` 内,越界返回 404。
- **新 A:frontmatter 换行 → 已修**:`_fm_scalar`/`_fm_list` 写入前拒绝含换行的标量/
  列表项,LLM 吐出的坏 label/keyword 在闸口报错,不产出不可解析的碎片。
- **新 B:rebuild 非原子 → 已修**:先 parse 所有碎片 + 联网重嵌(可失败的事全做完),
  全部成功才 `_clear` + 灌库;坏碎片/网络失败不再留下半清空的库。

## 待办(尚未处理)

#4 迁移器 `MAX(version)`、#5 rebuild 全量重嵌(原子性已改善,但仍每次重嵌)、
无 `busy_timeout`(并发写偶发锁)、`sweep_stale` 未接线(preview 缓存无限增长)、
`/api/transcripts` 冷缓存首次列表需 clean 全部文件(可加便宜预筛)。均不阻塞 S3。

**P1-A(GPT 复查 A 条,仍未处理)**:`chunk._normalize_segment` 把 agent 输出的反序/越界边界静默
交换+夹紧(`0-999`→`1-n`),坏边界仍 `run_chunk` 成功并写 staging。S5 第一段只补了 P1-B(段间重叠/
空洞)。建议改成对 agent 边界严校(整数回合号、落真实回合集、`start<=end`),坏即抛 `ValueError`
触发现有重试,不静默修。手动建段(`manual_segments`)的夹紧可保留。

## 当前实际状态

- 项目是 Python 包,入口命令是 `memory-system`。
- 已有 S1/S2 的一部分实现:SQLite 迁移、碎片读写、`index rebuild`、transcript 清洗预览、段级 processed 标记、本地前端。
- 前端到 S3:浏览 transcript、选段标已处理、**切块(运行 agent / 并分移边界 / 手动建段 / 标删 / 保存)**。
- S4 引擎完工:提取五件套(`extract` CLI / `POST /api/extract`)、按块回滚、落 staging。**S4 未做前端**。
- S5 第一段完工(引擎/API/CLI):staging→active 碎片 + 增量入库 + node 别名合并 + reject/archive;P1-B 已补。
  **下一步 = S5 第二段:富审核 GUI**(staging 审核/编辑/去噪/批量归档界面),然后 S6 检索。

## 已知隐患

### 1. README 漏写前端启动命令

代码里已经有 `serve` 命令:

- `memory_system/cli.py` 的 `cmd_serve`
- `memory_system/server.py` 的 `serve(cfg, host="127.0.0.1", port=8765)`

但 `README.md` 的 CLI 列表没有写 `memory-system serve`。这会让接手者以为前端不存在或需要另找启动方式。

建议:把 `memory-system serve` 加进 README,注明默认地址 `http://127.0.0.1:8765`。

### 2. 前端范围容易被误会

当前 localhost 前端不是完整审核前端,只是 S2 选段前端。

已有:

- transcript 列表
- transcript 清洗预览
- resume 断点展示
- 回合选择
- 标记选段为已处理

未有:

- 切块 agent
- 人工并段/分段/改边界
- 提取五件套
- staging 审核/编辑
- active/rejected/archived 流转

建议:在 README 或 UI 标题里写明“当前为 S2 选段工具”。

### 3. `covered_uuids` 注释和碎片规则冲突

`memory_system/fragments.py` 明确写了 uuid 绝不进碎片。

但 `memory_system/db/migrations/m003_processed.py` 的注释说 episode 落地后碎片会自带 `covered_uuids`,并可由 `index rebuild` 回填 processed 段。

这两处设计冲突。按当前概念方案与代码实际状态,更合理的是:

- uuid 不进 episode 碎片。
- `processed_segments` 只是 S2 操作态/书签。
- `index rebuild` 不负责恢复 processed 标记。
- processed 表丢了不影响记忆正本。

建议:改掉 `m003_processed.py` 的注释,避免后续实现把 uuid 写回碎片。

### 4. 迁移器用 `MAX(version)` 判断当前版本

`memory_system/db/migrate.py` 用 `MAX(version)` 当当前 schema 版本,`status()` 也按 `m.version <= cur` 判断是否已应用。

隐患:如果数据库出现“001 和 003 已应用,002 缺失”的坏状态,它会误报 002 已应用。

早期个人项目够用,但迁移器更稳的做法是按 `schema_migrations` 的实际 version 集合逐个判断,`up()` 也补齐缺失迁移。

建议优先级:中。等迁移数量继续增加前处理。

### 5. `index rebuild` 会重嵌所有 episode

`memory_system/index.py` 的 rebuild 会清空 DB,从碎片重灌,然后对所有 episode overview 调 embedding provider。

用 fake provider 时没成本;用 DashScope 时会真实联网、耗时、花费额度。

建议:

- README 明确 `index rebuild --provider fake` 适合测试。
- 真库 rebuild 前提示会重嵌。
- 后续可加 `--skip-vectors` 或向量缓存策略。

### 6. node 文件名可能碰撞

`memory_system/fragments.py` 用清洗后的 node label 前 80 字符作为文件名。

两个不同 label 清洗/截断后可能得到同一个文件名,导致覆盖。label 本身才是 node 身份,文件名只是可读句柄。

建议:node 文件名加短 hash 后缀,例如 `清洗后label__a1b2c3.md`。

### 7. server 接受任意 path 参数

`memory_system/server.py` 的 `/api/transcript?path=...` 直接读取传入 path,没有限制必须位于 `cfg.transcripts_root` 下。

虽然服务只绑 `127.0.0.1`,但仍建议做路径边界校验,减少本地误用或页面被诱导请求任意文件的风险。

建议:解析 realpath 后确认 `path` 在 `cfg.transcripts_root` 内。

### 8. resume 检测目前主要靠 uuid 重叠

`memory_system/resume.py` 通过“当前 transcript 开头回合的 uuid 是否都出现在更早 transcript”来识别复刻前缀。

这能工作,但它把 resume 判断绑定在 uuid 行为上。若未来确认存在更可靠的 `last-prompt` / `lastPrompt` / 平台 marker,可以改成 marker 优先、uuid 重叠兜底。

建议优先级:低到中。S2 通过门本来允许人审兜底。

## 建议处理顺序

1. 更新 README:补 `memory-system serve` 和 S2 前端范围。
2. 修正 `m003_processed.py` 关于 `covered_uuids` 的误导注释。
3. 给 server 加 transcript root 路径校验。
4. 给 node 文件名加 hash 防碰撞。
5. 改迁移器状态判断,从 `MAX(version)` 改成按实际 applied set。
6. 给 rebuild 增加真 embedding 成本提示或 `--skip-vectors`。

## GPT 的建议(2026-06-20 复查)

> 背景:在 S3 续写后做了一轮代码审查。`scripts/verify_s1.py`、
> `scripts/verify_s2.py`、`scripts/verify_s3.py` 用 `.venv/bin/python` 跑过,
> 三条全绿。主干链路不是失控状态;真正值得优先补的是"坏段边界被当成好结果"
> 和 fake provider 锁污染。

### A. agent 切块输出不应静默夹紧(P1)

位置:`memory_system/chunk.py` 的 `_normalize_segment`。

当前行为:agent 输出 `start/end` 后,代码会把反序边界自动交换,并把越界值夹进
`[1, n_turns]`。例如 `0-999` 会变成 `1-n`,`50-20` 会变成 `20-50` 后再夹紧。

隐患:模型一旦输出坏边界,`run_chunk` 仍会成功,随后 `covered_uuids` 会按这个被修过的
区间写入 staging。后续 S4 提取会以为这是人工/agent 确认过的真实材料范围。

建议:对 agent 输出严格校验:

- `start/end` 必须是整数回合号;
- 必须落在真实回合集合内;
- `start <= end`;
- 单段坏了就抛 `ValueError`,触发现有重试/失败告警,不要静默修。

### B. 保存段缺全局覆盖校验(P1)

位置:`memory_system/server.py` 的 `_api_save_segments`,以及前端 `memory_system/web/app.js`
的手动建段/移边界/并段逻辑。

当前行为:服务端只校验每个段的 `start_turn/end_turn` 是否存在,不校验段之间是否重叠、
是否有空洞、是否按顺序覆盖。前端也允许任意选中回合后用最小/最大回合建连续段,
即使中间并未被选中。

隐患:S3 的段是 S4 提取的工作输入。如果允许 `[1-10]` 和 `[8-20]` 同时保存,或漏掉
`11-12`,后续提取可能重复消费同一回合、漏提材料,或把用户以为没选的中间回合一起送入段。

建议:新增统一的 `validate_segments(segments, turn_idxs, *, require_full_cover=True)`:

- agent 成功结果建议要求从第一回合到最后一回合完整、无重叠、无空洞覆盖;
- 人工粗分如果设计上允许只保存子范围,也至少要禁止重叠,并在 UI 上明确显示 gap;
- 手动"选中回合→建段"最好只允许连续选择,或把不连续选择拆成多段;
- 保存前端与服务端都校验,服务端为准。

### C. `index rebuild --provider fake` 可能污染真库 embedding 锁(P2)

位置:`memory_system/index.py` 的 `assert_embeddable`,CLI 入口在 `memory_system/cli.py`
的 `cmd_index`。

当前行为:如果 meta 里还没有 `embedding_model/embedding_dim`,rebuild 会用当前 provider
落锁。README 又提示测试可用 `index rebuild --provider fake`。

隐患:用户在真实 `~/.memory_system` 上第一次跑 `--provider fake`,meta 会写成
`embedding_model=fake`。之后再用 DashScope 真重建会被模型锁拒绝,需要人工清 meta 或删库。
这也和 `memory_system/embedding/fake.py` 里"不要写进真库"的注释冲突。

建议:

- fake provider 只允许在测试 home 下写锁,或需要显式 `--allow-fake-lock`;
- 更稳妥:CLI 的 `--provider fake` 只做 dry-run / `--skip-vectors`,不写 `episode_vectors`
  和 embedding meta;
- README 里把"测试用 fake"限定为临时 `MEMORY_SYSTEM_HOME`。

### D. 迁移器 `MAX(version)` 仍建议补掉(P3)

位置:`memory_system/db/migrate.py`。

旧清单里已经写过,复查确认仍存在。`current_version()` 用 `MAX(version)`,
`status()` 用 `m.version <= cur`,所以坏状态 `001,003` 已记录但 `002` 缺失时,会误报
002 已应用,`up()` 也不会补齐。

建议:改成读取 applied version set:

- `status()` 按 `m.version in applied` 判断;
- `up()` 逐个应用未出现的迁移,并可在发现"高版本已应用但低版本缺失"时直接报错或补齐;
- `down()` 保持按实际已应用版本倒序回滚。

### E. 已验证为修复/不再适用的旧项

- server 任意文件读:当前 `_confine()` 已限制 path 必须落在 `cfg.transcripts_root` 内。
- node 文件名碰撞:当前 `_safe_node_filename()` 已加全 label sha1 后缀。
- `m003_processed.py` 的 `covered_uuids` 注释冲突:当前注释已改成"纯操作态,不参与碎片重建"。
- resume 检测:相关实现已删,不应再按旧条目继续修。
