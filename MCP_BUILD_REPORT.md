# MCP recall tools 施工报告

> 完工日期:2026-07-10  
> 状态:代码与离线验收完成;**尚未注册上线**。注册仍以 Phase A 召回达标门通过并由 zuris 显式拍板为前提。

## 1. 交付范围

本轮按 `../project/mcp_build_plan.md` 完成 M-1、M-2 与仓库内收口:

- 手写标准库 stdio JSON-RPC server,未引 MCP SDK 或任何新依赖。
- 三个 recall tool 全部薄调现有 `memory_system/recall/*`,未复制检索/重构语义。
- 每个 server 进程启动时生成一次 `mcp-<uuid4 前 12 hex>` session_key,只向 episode 路透传。
- 选路描述成为三份可版本化 prompt 文件,进入控制台八键白名单;`tools/list` 每次现读并有缺文件回退。
- 新增真实子进程离线验收,覆盖协议、三路业务、错误降级、session、stdout 和红线。
- 未实现明确排除项:resources、prompts 能力面、HTTP transport、listChanged、自动注入 daemon。

## 2. 文件变更

新增:

- `memory_system/mcp_server.py` — 最小 JSON-RPC 协议壳、三 tool 分派、业务降级、进程级 session。
- `memory_system/prompts/tool_episode_desc.txt`
- `memory_system/prompts/tool_detail_desc.txt`
- `memory_system/prompts/tool_concept_desc.txt`
- `scripts/verify_mcp.py` — 临时 home + fake provider + 真实 CLI 子进程验收。
- `MCP_BUILD_REPORT.md` — 本报告。

修改:

- `memory_system/cli.py` — 新增 `memory-system mcp`。
- `memory_system/recall/episode.py` — 唯一 recall 层改动:新增
  `injected_tool: str = "episode"`,CLI 保持 `episode`,MCP 传 `memory_recall_episode`。
- `memory_system/prompt_store.py` — 五键扩为八键,新增 `mcp:选路`,保留白名单、非空校验和原子写。
- `memory_system/server.py` — 同步 prompt API 的八键/生效边界说明;未改 recall Web 编排。
- `memory_system/web/console.js` — 新增选路分组和客户端缓存提示;MCP 描述保存状态显示“下一次会话生效”。
- `scripts/verify_web_api.py` — 八键、标签、选路描述原子替换与恢复门。
- `README.md` / `HANDOFF_NOTES.md` / `ARCHITECTURE.md` — 使用入口、职责、运行态 session、缓存边界与上线闸门。

`pyproject.toml` 未改:既有 `prompts/*.txt` package-data 已覆盖三份新文件。

## 3. 协议与 tool 接口

协议只实现:

- `initialize`:回显客户端 `protocolVersion`;capabilities 仅 `{"tools":{}}`。
- `notifications/initialized`:notification,不响应。
- `tools/list`:返回三个 tool;描述每次现读文件。
- `tools/call`:统一返回 `content:[{type:"text",text:"..."}]`。

| Tool | 可见参数 | 隐藏参数 | 行为 |
|---|---|---|---|
| `memory_recall_episode` | `query: string`(必填) | `raw: boolean` | 双路召回三槽;默认重构;raw 把结构化 JSON 字符串放在 text 中;传进程 session_key 并写真实 tool 名。 |
| `memory_recall_detail` | `query: string`(必填),`since?`,`until?`,`raw?` | 无 | 逐字检索;raw 返回整条 source_text;不调重构 agent。 |
| `memory_recall_concept` | `node: string`(必填),`context?` | `raw: boolean` | label/别名调档;默认重构;raw 把结构化 JSON 字符串放在 text 中。 |

所有 tools/call 都是主动回忆,显式 `touch=True`;raw 只改变返回形态,不改变 touch。

错误分层:

- 坏 JSON `-32700`;未知 method `-32601`;缺参/错类型/未知 tool/多余参数 `-32602`。
- episode/detail 空手、concept miss、`ChatError`、meta 锁不符、`EmbeddingError` 都返回正常 tool result。
- ChatError 返回一行说明 + 结构化 JSON;EmbeddingError 返回人读配置提示。单次失败后 server 继续读下一行。
- stdout 只写逐行 JSON-RPC;日志走 `logs_dir` 文件或 logging 的 stderr handler。

## 4. 验收结果

终跑前均清理代理变量并设置 `no_proxy=127.0.0.1,localhost`。核心四门原文要点:

- `.venv/bin/python scripts/verify_mcp.py` — `verify_mcp: ALL GREEN`
  - `JSON-RPC、三 tool、描述同进程热读/缺文件回退、raw/miss 与 stdout 纪律`
  - `同进程 session 硬去重、episode 空手、tool 列与跨进程新 session`
  - `空库、ChatError/EmbeddingError 注入与真实子进程降级,失败后 server 继续响应`
- `.venv/bin/python scripts/verify_s6.py` — `S6 检索层 ALL PASS ✅`
- `.venv/bin/python scripts/verify_ranking.py` — `verify_ranking: 全部通过`
- `.venv/bin/python scripts/verify_web_api.py` — `Web API staging contract ALL PASS ✅`;
  `POST /api/recall 门 ALL PASS ✅`

扩展回归也全部 exit 0:

- `verify_s1.py` — `ALL PASS ✅`
- `verify_s2.py` — `S2 引擎层 ALL PASS ✅`
- `verify_s3.py` — `S3 引擎层 ALL PASS ✅`
- `verify_s4.py` — `S4 提取层 ALL PASS ✅`
- `verify_s5.py` — `S5 审核/归档层 ALL PASS ✅`
- `verify_delete.py` — `删除层 ALL PASS ✅`
- `verify_edit.py` — `编辑写回层 ALL PASS ✅`
- `verify_view_api.py` — `View API read contract ALL PASS ✅`
- `verify_provider_config.py` — `Provider config regressions ALL PASS ✅`

静态门:

- `python -m py_compile` 通过。
- `node --check memory_system/web/*.js` 全通过。
- Web JS NUL 字节计数 = `0`。
- `git diff --check` 通过。
- `.venv/bin/memory-system --help` 已列出 `mcp` 子命令。
- prompt 热读/原子写测试后原文已恢复,无验证标记或 `.tmp` 残留。

## 5. 注册与 SessionStart 示例

Phase A 达标且 zuris 明确放行后,用户级注册命令:

```bash
claude mcp add --scope user memory -- <repo>/.venv/bin/memory-system mcp
```

SessionStart 与 MCP 独立;仓库只记录了读取 `opening_cache/global.md` 的用户级 hook 示例,
本轮未修改 `~/.claude/settings.json`。

## 6. 偏离与边界决定

1. **未执行注册或修改用户配置**:这不是漏项。施工正本明确规定注册受 Phase A 达标门和 zuris
   显式拍板约束;本轮只提供命令与 hook 示例。
2. **未回写仓库外施工文件**:`../project/mcp_build_plan.md`、`../project/recall_tools_plan.md` 和
   `../project/out/mcp_log.md` 在当前沙箱的只读范围,无法按施工正本追加勾账/工单日志。M-1/M-2 的
   改动、决定、验收与遗留已完整集中到本报告和仓库内 HANDOFF/ARCHITECTURE,没有越权写外部目录。
3. **EmbeddingError 真实子进程门不联网**:除 fake 主夹具外,该门临时选择 dashscope provider 但
   故意移除 key,在发出网络请求前稳定抛 `EmbeddingError`;ChatError 同理用缺失 DeepSeek key。
   两条都在临时 `MEMORY_SYSTEM_HOME` 内运行,随后验证同一进程继续响应。
4. **未提交 git commit**:遵守本轮明确禁令。原有未跟踪 `review_ideas.md` / `suggest.md` 保持未改。

