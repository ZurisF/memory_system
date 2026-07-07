# S6 历史笔记

> 用途:归档 S6 检索层的施工脉络、评测基建、Phase 2 收口和 MCP 前置。
> 当前架构事实看 `../ARCHITECTURE.md`;当前交接看 `../HANDOFF_NOTES.md`。

## Phase 1 完工(2026-07-02)

施工书:`../project/s6_build_plan.md`。代码集中在 `memory_system/recall/`,CLI 入口:
`recall {detail,episode,concept}` + `opening {rebuild,show}`。

| 模块 | 作用 | 关键裁定 |
|---|---|---|
| `detail.py` | FTS5 trigram grep + snippet 开窗 | 无 embedding/LLM/衰减;命中刷时钟 |
| `episode.py` | 向量+FTS -> RRF -> 衰减 -> 三槽 | 只刷 top-1 + 同源;联想不刷 |
| `concept.py` | node/alias 精确解析 -> 膜 join 全量取 | 这是「取」不是「搜」;只刷 node 时钟 |
| `decay.py` | 惰性衰减现算 | 不落库;改配置即时影响全库 |
| `reconstruct.py` | 结构化槽位 -> 自然语言 | 候选集程序定死,LLM 只做表达 |
| `opening.py` | 开场注入 cache | 窥视不回忆,全程不刷时钟 |

配套:
- `AgentConfig.recall_model` 默认 sonnet。
- `--json` 输出结构化契约,不调 LLM;`--raw` 是人类可读调试逃生口。
- `eval/queries.jsonl` + `scripts/eval_recall.py` 用于 hit@k、A/B 参数和只读评测。
- SessionStart hook 只读 `opening_cache/global.md`,仓库内施工到 `opening show` 为止。

## 评测基建(2026-07-05)

施工书:`../project/eval_build_plan.md`。

- `scripts/eval_gen.py`:用 mimo 批量生成合成语料与题目,逐行校验,坏行落 rejects。
- `scripts/eval_ingest.py`:validate / prep-queries / ingest / reset。reset 把碎片移入 backup,不直接删。
- `scripts/eval_recall.py`:负例指标、按 mode/type 分组、MRR、verbose miss、JSON 留档。
- `POST /api/recall` + 前端第四视图 `web/recall.js`:结构化结果 + 可选重构,touch 默认 false。

## Phase 2 完工(2026-07-07)

施工书:`../project/s6_phase2_build.md`,施工日志:`../project/out/s6p2_log.md`。
四个工单已落地:

1. **episode session 去重 / 跨 session 冷却**
   新表 `injected_log` 存 `session_key/public_id/tool/hit_at`;同 session 已注入条目硬排除,
   其他 session 窗口内注入条目乘 `cooldown_factor` 软降序。无 `session_key` 时完全回到 Phase 1 行为。

2. **召回时别名露出**
   episode 三槽条目如果 source_text 里出现 node alias 且未出现规范 label,附
   `alias_bridges` 桥接行。它和 concept 入口的 `alias_bridge` 是两件事。

3. **开场槽 C(spark)**
   `opening.py` 扩为 latest / ballast / spark 三槽。spark 采样权重:
   `salience_tier * (1 - activation) + 0.05`,温度采样;`opening_spark=0` 回 Phase 1。

4. **recall 专用 provider 通道**
   `AgentConfig.recall_provider` 空串表示跟随全局。唯一解析点在 `reconstruct.run()`;
   `/api/agent/config` 和控制台 recall 卡支持 model + provider。

新增旋钮:
- `MEMORY_RECALL_DEDUP_SESSION`
- `MEMORY_RECALL_COOLDOWN_HOURS`
- `MEMORY_RECALL_COOLDOWN_FACTOR`
- `MEMORY_RECALL_OPENING_SPARK`
- `MEMORY_RECALL_OPENING_SPARK_TEMP`
- `MEMORY_AGENT_RECALL_PROVIDER`

注意:坏值回落只覆盖半衰期解析和 Phase 2 新旋钮;早期 topk/rrf/窗口等参数仍是直接
`int/float` 解析。

## MCP 前置

下一步施工书:
- `../project/recall_tools_plan.md`
- `../project/mcp_build_plan.md`

当前仓库尚未落地 MCP server。计划中的 MCP server 会:
- 每进程/连接生成运行态 `session_key`。
- episode tool 调 `recall_episode(..., session_key=...)`,成为 injected_log 去重/冷却的第一个真实消费方。
- `injected_log.tool` 从 CLI 的 `'episode'` 扩展为真实 MCP tool 名。
- 三个 tool 描述文案进入 prompt 控制台(`tool_*_desc.txt`),但 MCP 客户端通常按连接缓存 tools/list。

上线前置仍是召回达标门,需要 zuris 显式拍板。
