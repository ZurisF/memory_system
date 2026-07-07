# memory_system 接手笔记

> 目的:让后续实例快速看清真实状态、下一步、验证方式、高优先风险。
> - 架构全貌 → **`ARCHITECTURE.md`**(认知正典:分层/接口/铁律/数据流)。
> - S5 写入侧细节(语义/前端坑/验收门/工程债) → **`S5_NOTES.md`**。
> - S6 检索层施工书 → `project/s6_build_plan.md`(裁定与参数依据都在里面);Phase 2 施工书 →
>   `project/s6_phase2_build.md`(§0 裁定/§6 MCP 化次序),施工日志 → `project/out/s6p2_log.md`。
> - 概念正典 → `project/idea_v2.md`。
> 本文件只写「现状 + 交接 + 下一步」,刻意保持清爽。最近整理:2026-07-07(S6 Phase 2 完工收口)。

## 当前状态

- **S1–S5 引擎 / API / CLI / 前端全绿**。写入侧(切段 → 蒸馏 → 提取 → 审核入库)、查看侧只读
  galaxy、控制台(provider 配置/自定义/key 掩码/连接测试)、三视图导航,全部就绪。
- 记忆正本是 `fragments/` 碎片;`memory.db` 是可重建索引(vec0 向量 + FTS5 trigram + 概念图)。
  三条铁律(碎片是正本 / uuid 不上台面 / key 不落盘)见 `ARCHITECTURE.md §2`。
- **provider 知识已收口**到 `agent/registry.py`(单一来源,2026-06-25);server.py 随之瘦身。
- 四项工程债 + 边计算性能标记已修(2026-06-25),详见 `S5_NOTES.md`。
- **P2 前端全局已收口**(2026-06-26):`state.js` ~22 个裸全局收进单一 `ST = {}` 命名空间(轻量路线,
  非 ES module),8 个 JS 机械改引用。工程债清单见 `ARCHITECTURE.md §10`(P2 已移入「已完成」)。
- **蒸馏区提取并发 + 逐条落盘**(2026-06-26):`extract_segments` 走线程池(慢 I/O 并发、落盘回主线程
  串行),每段一完成即落 staging,**中途退出已完成的不丢**;失败段卡内联错误 + 「忽略」关闭(清 retry)。
- **删除写回(后端 + API + 前端,全做完)**(2026-06-26):`archive.delete_episode` / `delete_node` 真删误入库的
  episode / node(碎片正本 + DB 同步,删除顺序与 confirm 相反、删后 rebuild 不复活)。CLI `delete {episode,node}` +
  `DELETE /api/memory|node` + galaxy 详情面板删除按钮(二次 confirm、删 node 提示影响 N 条 episode、删后刷新图)。
  门:`verify_delete.py`(引擎)+ `verify_view_api.py` 新增 3 道 HTTP 删除门。
- **编辑写回(后端 + API + 前端,全做完)**(2026-06-26):`editor.edit_episode`(+ `EditError`/`EditReport`)改正本
  正文四件(overview/summary/highlights/salience_tier);**改 overview 才重嵌**(省额度),落地顺序同 confirm
  (DB commit 成功后才回写碎片),no-op 不写盘,白名单挡 source_text/nodes。`POST /api/memory/edit` +
  galaxy episode 面板编辑态。门:`verify_edit.py`(引擎)+ `verify_view_api.py` 1 道 HTTP 编辑门。
  **下一轮候选**:① 概念图(nodes 膜)编辑——给 episode 增删 node;② 孤儿 episode(删 node 后 0 挂载、
  galaxy 看不见但仍在库)的可见化/重指派(删 node 时的镜像问题,zuris 已知);③ S6 检索层。
- **蒸馏屏 nodes 改结构化行**(2026-06-27):S4 五件套编辑器里 nodes 从裸 JSON textarea 改成结构化行
  (label + action 下拉[新建/命中已有/记为别名] + 别名输入[仅 add_alias 档出现] + 理由 + 删),
  `collect()` 还原成原 `{label,action,reason,new_alias}` 形状,**后端零改动**(白名单/`_plan_nodes` 不变)。
  纯前端 `triage.js`(三处)+ `styles.css`(一处),`node --check` 过、无 NUL。
- **健壮性一轮(2026-07-01,全代码审查后修复)**:碎片正本原子写(tmp+`os.replace`,含 .env/
  custom_providers/预览缓存);工作态并发锁(新模块 `locks.py`,segments/staging 按 session 互斥,
  registry.CUSTOM_LOCK 保 provider 目录与 cfg 热改);`GET /api/agent/config` 不再 override=True
  (shell export 的真 key 不会被 .env 占位冲掉);列表热路径不再全量数行(`describe` 默认
  `count_lines=False`,`line_count` 从列表 API 退场);rebuild 清空+重灌单事务;前端修一处属性
  逃逸 XSS(段卡 tag `escAttr`)、裸 fetch 全兜底、三处补 `once()`、切换 transcript 后发者胜。
  明细见 `ARCHITECTURE.md §10` 已完成首条。全套 verify 绿。
- **S6 检索层 Phase 1 完工**(2026-07-02,commit `57f2838`):三路检索 + 重构 agent + 惰性衰减 +
  开场注入 + 评测夹具,详见下节。`verify_s6.py` 33 断言全绿,s1–s5 及 web/view/provider 全套回归绿。
- **控制台补充**(2026-07-02,commit `47430d6`):`/api/agent/config` 增 **recall 角色**(仅 model,
  写 `MEMORY_AGENT_RECALL_MODEL`;当时**无专用 provider 通道**——Phase 2 已补,见下方
  2026-07-07 条);新建 `prompt_store.py` +
  `GET/POST /api/prompts` —— 三过程五个 system prompt(chunk/extract/recall_episode/recall_concept/
  opening)控制台在线编辑,五键白名单文件名写死(堵路径穿越)、content 非空校验、tmp+`os.replace`
  原子写、写后清 chunk/extract 的 `lru_cache` 即时生效(重构每次现读无缓存)。门:`verify_web_api`
  加 prompts 四道(含 lru 刷新硬证明)、`verify_provider_config` 加 recall 三道。
- **召回评测基建完工**(2026-07-05,施工书 `project/eval_build_plan.md`,数据契约/裁定都在那份):
  ① `scripts/eval_gen.py` —— mimo(小米,OpenAI 兼容,key 走环境变量 `mimo_api_key`,模型 mimo-v2.5)
  批量生成合成语料与题目:按 `eval/clusters.jsonl` 簇清单分批调用、逐行校验、坏行落 rejects.log、
  断点续跑;生成 prompt 在 `eval/prompts/`(出题 prompt 结构性看不到 overview,防作弊题)。
  ② `scripts/eval_ingest.py` —— validate(一次报全)/ prep-queries(出题输入,无 overview 泄漏)/
  ingest(临时 id→ep_syn####、时间戳均匀铺过去 365 天、rebuild、改写夹具、落 manifest)/
  reset(碎片**移入** backup_<UTC>/ 再重建空库,绝不删)。评测期直接用真库,用 reset 清。
  ③ `scripts/eval_recall.py` 升级 —— 负例(expect=[])独立指标(误召回均值/干净占比)、按 mode×type
  分组小结、MRR(miss 计 0)、`--verbose` miss 明细、`--out` JSON 留档(含 RecallConfig 快照)。
  ④ `POST /api/recall` + 前端第四视图「召回」屏(`web/recall.js`):左结构化槽位卡片、右一键重构,
  touch 默认 false;ChatError 折成 200+error 降级。门:verify_web_api 加 recall 7 道。
  另修 verify_web_api 一处老 flaky(staging 编辑门押 `episodes[0]`,顺序随并发提取完成序漂移,
  改按 stage_id 找)。运行手册 `eval/README.md`;全套 verify + 三个 selftest 全绿。
- **S6 检索层 Phase 2 完工**(2026-07-07,四 commit:P2-1 `bef426c` / P2-2 `da75117` /
  P2-3 `bceeb3f` / P2-4 `eeef776`):Phase 1 四项留口全部落地,逐项实况见下节「Phase 2 留口」,
  每工单文件清单/关键决定/偏离见 `project/out/s6p2_log.md`(P2-4 曾因沙箱产物丢失重做一次,
  日志里有订正节)。全量回归 verify_s1–s6 + verify_provider_config + verify_web_api 全绿。
  新增旋钮(全走 env,坏值回落默认):`MEMORY_RECALL_DEDUP_SESSION` /
  `MEMORY_RECALL_COOLDOWN_HOURS` / `MEMORY_RECALL_COOLDOWN_FACTOR` /
  `MEMORY_RECALL_OPENING_SPARK` / `MEMORY_RECALL_OPENING_SPARK_TEMP`、
  `MEMORY_AGENT_RECALL_PROVIDER`(空串=跟随全局)。
- 前端仍是零构建原生静态资源,各 JS 模块职责单一,可单独改。

## S6 检索层 Phase 1(已完工,2026-07-02)

> 施工书 `project/s6_build_plan.md` 八步全做完;裁定(时钟规则/槽位/确定性边界)与参数依据都在那份文档,
> 本节只留地图。设计探讨的历史稿是 `project/s6_retrieval_design.md`(已被施工书吸收,按施工书为准)。

代码全在 `memory_system/recall/`,CLI 入口 `recall {detail,episode,concept}` + `opening {rebuild,show}`:

| 模块 | 做什么 | 关键裁定 |
|---|---|---|
| `detail.py` | FTS grep + snippet 开窗(`--raw` 逐字原文,`--since/--until`) | 无 embedding/LLM/衰减;命中刷时钟 |
| `episode.py` | 向量+FTS 双路 → RRF(只用名次)→ active 硬过滤 → 衰减乘子 → 三槽(主/同源/联想) | 只刷 top-1+同源;联想不刷(被联想≠被回忆) |
| `concept.py` | label→alias 精确解析(miss 给子串建议)→ 膜 join **全量**取 active(这是「取」不是「搜」) | 不取 source_text;只刷 node 时钟;经 alias 进来带 `alias_bridge` 桥接行 |
| `decay.py` | 惰性衰减:活跃度现算 `0.5^(天/半衰期)`,半衰期按 tier 14/90/365 天 | 不落库、不 commit(谁调用谁 commit);改配置全库即时生效 |
| `reconstruct.py` | 重构 agent:结构化槽位 → 一段自然语言(三部分输入铁律) | **候选集程序定死、调用前写日志可重放**;LLM 只做表达;失败抛 ChatError 由 CLI 降级结构化输出(退出码 3) |
| `opening.py` | 开场注入:槽 A 最新 1 条 + 槽 B tier≥2 活跃度最高 1–2 条,原子写 `opening_cache/global.md` | **窥视不回忆,全程不刷时钟**;dirty 标记接 confirm/archive/delete/edit 四点,rebuild 只在 dirty 时干活 |

配套:
- **配置**:`RecallConfig`(半衰期/topk/RRF k/衰减权重/槽位宽度/开窗宽度/开场预算,全走
  `MEMORY_RECALL_*` 覆盖,坏值回落默认不炸检索;Phase 2 追加去重/冷却/火花五旋钮,见下节);
  `AgentConfig.recall_model` 默认 sonnet(候选集已定死、重构只做表达,检索路径求快省),
  Phase 2 追加 `recall_provider`(空串=跟随全局)。
- **逃生口**:`--json` = §5 结构化契约(机器可读,不调 LLM);`--raw` = 人类可读结构化渲染(调试)。
- **评测夹具**:`eval/queries.jsonl`(zuris 随真实记忆积累手工添加)+ `scripts/eval_recall.py`
  (hit@k、`--param` A/B 对比、`touch=False` 只读不污染时钟)。
- **红线贯彻**:所有对外输出手工挑字段,只 public_id / node label,无 uuid/向量/DB 整数 id;
  所有排序带 tie-break(public_id/created_at),同一库同一 query 结果可重放。
- SessionStart hook 只读 `opening_cache/global.md`(毫秒级),**hook 接线在仓库外**,施工到
  `opening show` 为止。

**Phase 2 留口 → 已全部完成(2026-07-07)**(施工书 `project/s6_phase2_build.md`,细节
`project/out/s6p2_log.md`):
- episode 检索的 **session 去重 / 跨 session 冷却** — 已完成。新表 `injected_log`(m004,存
  public_id 不存整数 id,**rebuild 不清**)记录三槽注入;同 session 已注入的从三槽候选**硬排除**,
  其他 session 在 `cooldown_hours` 窗口内注入过的 final 分乘 `cooldown_factor`(默认 0.8,软降序
  不排除)。仅作用 episode;**无 session_key = 全关**(CLI 默认不带,行为同 Phase 1,零日志),
  `touch=False`(eval)不写日志。CLI `recall episode --session KEY`。
- **召回时的别名露出(grep 锚定)** — 已完成。三槽每条 episode:所挂 node 的 alias 字面出现于
  库内 source_text 且规范 label 未出现 → 附 `alias_bridges`(「文中「弥赛亚」= 概念 AGI」,
  无桥接省略该键);随槽位进重构输入,prompt 教 agent 当词义锚点。concept 入口的
  `alias_bridge`(入口解析)不动,两者语义不同。
- 开场**槽 C(温度采样火花)** — 已完成。槽位扩为 latest/ballast/spark,权重
  `w = salience_tier×(1−activation)+0.05`(重要且沉睡),`p ∝ w^(1/T)` 温度采样;
  `opening_spark=0` 回 Phase 1;`opening_max_items` 硬顶不变,spark 开启保留 1 席;
  **全程不刷时钟**;verify 注入固定 seed,生产走系统熵。
- 控制台 recall **专用 provider 通道** — 已完成(后端+前端)。`AgentConfig.recall_provider`
  空串=跟随全局;**单一解析点在 `reconstruct.run()`**(episode/concept/opening 共用,一处接线);
  `/api/agent/config` recall 节 GET 如实回 override 原值、POST 以 `"provider" in body` 区分
  「未传」与「传空串清 override」;console.js recall 卡 provider 下拉(首项「跟随全局」),
  连接测试复用既有通道;删 custom provider 连带清悬空 recall_provider。门:
  `verify_provider_config` 新四段。
- 性能标记(**仍留**,刻意延后):联想槽与 concept 的 context 排序用应用层 Python 算 L2(`_l2`),
  库大了要下沉 SQL/vec0。

**下一步 = MCP 化**(施工书 `project/recall_tools_plan.md`;次序:Phase A 喂库/达标门 →
Phase B MCP server,B 的注册上线以 A 的达标门为前提)。`s6_phase2_build.md §6` 记的增量:
① 三个 tool 描述文案(选路 prompt)进控制台在线编辑(`prompts/tool_*_desc.txt` 三新文件 +
`prompt_store` 白名单 +3;已知边界:MCP 客户端按连接缓存 tools/list,编辑后下一次会话生效);
② MCP server 每连接(initialize)生成 session_key,tools/call episode 路透传——P2-1 机制的
第一个真实消费方,`injected_log.tool` 届时填真实 tool 名。
其余候选(未定,问 zuris):概念图(nodes 膜)编辑——给 episode 增删 node;孤儿 episode
可见化/重指派(删 node 后 0 挂载,galaxy 看不见但仍在库);SessionStart hook 真接线(仓库外)。

## 高优先风险

- **FTS trigram 最短 3 字**:中文 2 字词索引不到。detail/episode 的 FTS 路对短 query 空手
  (episode 退化向量单路;detail 空结果时 CLI 提示换长词,不静默)。
- **recall episode / concept --context 要联网**:查询向量走真 embedding provider(DashScope),
  且过 meta 锁校验(模型/维度与库内不符**拒检**);离线测试用 fake provider + fake 建的库。
  重构(默认输出)还要过 chat provider;`--raw/--json` 不调 LLM。
- **`last_accessed_at` 是运行态**:`index rebuild` 重置为 `activated_at`,衰减时钟会「回春」——
  这是设计内(时钟非正本),但调参/评测跨 rebuild 时别拿它当稳定信号。

- **自定义 provider base_url 配错** → LLM 调用 HTTP 405。控制台加 provider 时有校验提示(缺 `/v1`、
  常见平台域名误用)但非强制拦截。出问题先查 `~/.memory_system/custom_providers.json` 的 `base_url`。
- **迁移器旧库坏状态**:迁移器已改读实际行集合(`applied_versions`),但旧库若已缺中间版本需手补。
- **`/api/transcripts` 冷缓存首次会 clean 全部 jsonl**,大库下慢。
- **`index rebuild` 全量重嵌 overview**;真 DashScope 会联网、耗时、耗额度。
- **本机代理坑**:跑起本地 `ThreadingHTTPServer` 的测试(web_api / view_api / provider_config)前,
  必须 `export no_proxy=127.0.0.1,localhost` 且 `unset http_proxy https_proxy all_proxy`,否则 urllib
  把 localhost 也走代理 → HTTP 502。

## 验证

后端回归(优先用项目 `.venv`,否则可能因包路径或 `sqlite_vec` 未装失败):
```bash
.venv/bin/python scripts/verify_s1.py   # … s2 s3 s4 s5
.venv/bin/python scripts/verify_s6.py   # S6 检索层(衰减/三路检索/重构/开场,fake 离线)
.venv/bin/python scripts/verify_web_api.py
.venv/bin/python scripts/verify_view_api.py
.venv/bin/python scripts/verify_provider_config.py
```
检索质量评测(非通过门,调参用):`.venv/bin/python scripts/eval_recall.py`(读 `eval/queries.jsonl`,
hit@k,`--param KEY=V` 做 A/B,全程 `touch=False` 只读)。
前端语法 + NUL 检查、浏览器烟测:见 `S5_NOTES.md §前端注意 / §验收命令`。

## 文档可信度

可作当前依据:
- `ARCHITECTURE.md` — 架构认知正典(分层/接口/铁律/数据流/schema/API)。**先读这份。**
- `S5_NOTES.md` — S5 写入侧语义、前端坑、验收门 + 2026-06-25 工程债与 registry 重构记录。
- `project/s6_build_plan.md` — S6 施工书(裁定/参数/契约正本,S6 代码照它施工)。
  `project/s6_retrieval_design.md` 是它的前身探讨稿,冲突处以施工书为准。
- `project/s6_phase2_build.md` — S6 Phase 2 施工书(已完工勾账);施工日志
  `project/out/s6p2_log.md`(注意 P2-4 旧节有「产物未落盘」订正,以重做节为准)。
- `README.md` — 与当前代码接近。
- `project/idea_v2.md` — 概念正典。
- `project/frontend_plan.md` — 前端施工书,§7 API 契约、§8 数据对象形状。
- `project/prompts_extraction.md` — Prompt 2 基本可信;Prompt 1 早期文字写了行号,真实打包已改回合制。

历史参考,**不要按它施工**(含 `claude-memory`、`~/.claude-memory`、cron、FastAPI/HTMX、Ollama、
activation decay 等与当前方向冲突的旧设定):
`project/plan_v3.md`、`project/out/plan_v1.md`、`project/out/plan_v2.md`、`project/out/idea.md`。
