# MCP build plan 审阅建议

> 2026-07-07 Codex 只读复核记录。本文是给后续 Claude / Fable 施工实例看的建议清单,不代表已经落地。

## 总体判断

`../project/mcp_build_plan.md` 可以作为施工书使用。MCP 方案保持薄 stdio JSON-RPC 壳、不引 MCP SDK、不复制 recall 检索语义、验收走离线 fake provider,这些方向都合理。S6 Phase 1/2 可按已完成看待;下一步未完成的是 MCP recall tools 本体。

## 必须优先处理

1. `injected_log.tool` 目前按计划无法直接落地。
   - build plan 要求 MCP episode 调用写 `memory_recall_episode`,CLI 路径继续写 `episode`。
   - 当前 `memory_system/recall/episode.py` 的 `recall_episode()` 在写 `injected_log` 时 SQL 硬编码 `'episode'`。
   - 建议给 `recall_episode()` 增加一个很小的可选参数,例如 `injected_tool: str = "episode"`,MCP wrapper 传 `"memory_recall_episode"`。这样仍然保持 MCP 薄封装,不用复制检索或写日志逻辑。

2. MCP path 应明确 catch `EmbeddingError`。
   - build plan 已写 meta 锁不符要正常返回人读文本,但 embedding 不可用也应同样降级。
   - HTTP recall API 已经 catch `EmbeddingError`;MCP 不应让 provider 配置问题导致 stdio server 裸异常退出。
   - 建议 `verify_mcp.py` 增加一个 provider/embedding 失败场景。

## 验收边界补强

1. JSON-RPC 基础契约建议写进 `verify_mcp.py`。
   - 每个 response 都应包含 `jsonrpc: "2.0"`。
   - response `id` 必须回显 request `id`。
   - notification,例如 `notifications/initialized`,不应产生响应。
   - 未知 method 用 `-32601`,参数错误用 `-32602`,坏 JSON 用 `-32700`。

2. `raw=true` 的 MCP 返回形态需要定死。
   - MCP tool result 通常仍是 `content:[{type:"text", text:"..."}]`。
   - 建议约定 raw 结构化结果放在 text 中作为 JSON 字符串,并由 `verify_mcp.py` 解析断言。

3. “返回无整数 id”红线不要写成禁止所有数字。
   - 合法输出里会有 `score`、`salience_tier`、时间等数字。
   - 建议检测敏感 key / 形态,例如 `id`、`episode_id`、`node_id`、`rowid`、`embedding`、`uuid`,以及 UUID 格式字符串。

4. stdout 纪律要测真实子进程输出。
   - stdout 是 JSON-RPC 协议信道,不能有 `print` 或日志污染。
   - `verify_mcp.py` 应读取每一行 stdout 并断言都是合法 JSON-RPC response;日志只进 `logs_dir` 文件或 stderr。

## 容易踩坑点补充

1. 不要把 M-1 顺手扩成完整 MCP 平台。
   - 本轮只承诺 initialize / notifications/initialized / tools/list / tools/call 四件事。
   - 不做 resources/prompts、HTTP transport、listChanged、自动注入 daemon 或并发压力框架;这些都已被 build plan 明确排除。
   - `verify_mcp.py` 也只测这个最小子集,不要写成 MCP 协议认证套件。

2. 协议错误与业务失败要分层。
   - 坏 JSON、未知 method、参数缺失/类型错走 JSON-RPC error。
   - 查不到记忆、concept miss、detail 空手、重构 ChatError、meta 锁/embedding 配置问题,都应作为正常 tool result 返回人读文本。
   - 单次 tools/call 异常不能让 stdio server 退出;返回后继续处理下一行请求。

3. `session_key` 是运行态,不要落盘也不要绑定 initialize 时序。
   - 进程启动即生成一次,initialize 前如果收到 tools/call 也照常可用。
   - 只给 episode 路传 `session_key`,concept/detail 不写 `injected_log`。
   - `raw=true` 只改变返回形态,不改变 touch 语义;tools/call 仍代表主动回忆。

4. MCP 返回形态保持统一。
   - 无论自然语言重构、raw 结构化、空手引导、降级结果,都包成 `content:[{type:"text", text:"..."}]`。
   - raw 结构化建议是 text 里的 JSON 字符串,不要混用额外顶层字段,否则客户端兼容性会变差。

5. 离线验收要隔离真实库和真实 key。
   - `verify_mcp.py` 应使用临时 `MEMORY_SYSTEM_HOME`、fake embedding/chat provider 和可控 fixture。
   - 不读写用户的 `~/.memory_system`,不依赖网络,不消耗真实 embedding/chat 额度。
   - fixture 至少覆盖:命中、空手、concept miss、raw、重构失败、同进程 session 去重、换进程新 session。

6. tool 描述是选路提示,但不是实时控制通道。
   - server 侧 tools/list 可以每次现读 desc 文件;客户端仍可能按连接缓存。
   - 因此不要为了“编辑后立即推送”去实现 listChanged;把“下一次会话生效”的边界写清楚即可。

7. 日志与调试输出要从一开始就守住。
   - 不只 `mcp_server.py` 不能 print,它调用的 helper 也不应往 stdout 写进度。
   - 调试信息统一走 `logs_dir` 文件日志;verify 用真实子进程 stdout 检查,比单元测试 mock 更能抓手滑输出。

## M-2 prompt-store 注意点

1. 新增 `tool_episode_desc.txt` / `tool_detail_desc.txt` / `tool_concept_desc.txt` 后,`prompt_store.py` 中写死“五个 prompt”的 docstring/comment 要同步更新。
2. `pyproject.toml` 目前已有 `prompts/*.txt` package-data,新 desc 文件会被覆盖到包内,这里看起来无需额外改动。
3. `tools/list` 每次现读 desc 文件、不缓存,但 MCP 客户端可能按连接缓存 tools/list;这个边界已经写进 build plan,实现和 README/HANDOFF 需要保持一致。

## 结论

计划不算臃肿,更像一份可执行 checklist。真正会卡施工的是 `recall_episode()` 的 `tool` 硬编码;其余主要是防止 MCP 协议边界和降级行为漏测。
