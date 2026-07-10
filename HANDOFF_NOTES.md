# memory_system 接手笔记

> 目的:让后续实例快速看清真实状态、下一步、验证方式和高优先风险。
> 架构事实看 `ARCHITECTURE.md`;历史施工过程看 `history_notes/`。
> 最近整理:2026-07-10(MCP recall tools 落地,注册仍受 Phase A 达标门约束)。

## 当前状态

- **S1-S6 主链路已完成**:写入侧(切段 -> 蒸馏 -> 提取 -> 审核入库)、查看侧 galaxy、
  召回屏、控制台(provider/模型/prompt 配置)都已落地。
- **记忆正本是碎片**:`fragments/` 下 Markdown 是真相,`memory.db` 是可重建索引
  (vec0 向量 + FTS5 trigram + 概念图)。三条铁律见 `ARCHITECTURE.md §2`。
- **S6 Phase 2 已完成**:episode session 去重/跨 session 冷却、`alias_bridges`、开场 spark 槽、
  recall 专用 provider 通道均在代码中。细节归档到 `history_notes/S6_NOTES.md`。
- **MCP recall tools 已落地但未注册上线**:`memory-system mcp` 提供 episode/detail/concept 三个
  stdio tool,协议面只含 initialize / initialized / tools/list / tools/call。每个 server 进程生成一次
  `mcp-<12hex>` session_key,episode 注入日志的 tool 列写 `memory_recall_episode`;不落配置、不经客户端传 key。
- 三份 `prompts/tool_*_desc.txt` 已进控制台「选路」分组;server 每次 `tools/list` 现读并在文件缺失时
  回退硬编码。客户端通常按连接缓存 tool 列表,编辑后的描述一般到下一次会话生效。
- 前端仍是零构建原生静态资源,四视图:写入 / 查看 / 召回 / 控制台。

## 下一步

1. **MCP 上线裁定**:用合成语料/真实题跑 Phase A 召回达标门;达标后仍需 zuris 明确拍板,再执行
   用户级 `claude mcp add`。代码施工已完成,不能把 verify 全绿等同于注册放行。
2. **概念图编辑**:给 episode 增删 node、孤儿 episode 可见化/重指派。
3. **SessionStart hook 真接线**:仓库外配置,只读 `opening_cache/global.md`,与 MCP 独立。

## 高优先风险

- **FTS trigram 最短 3 字**:中文 2 字词索引不到。detail 空结果时换更长字面词;episode 会退化向量单路。
- **recall episode / concept --context 要联网**:查询向量走 embedding provider,且受 meta 锁约束;
  默认重构还要 chat provider。`--raw` / `--json` 不调 LLM。
- **`last_accessed_at` 是运行态**:`index rebuild` 会重置到 `activated_at`,跨 rebuild 不要拿它做稳定评测信号。
- **部分 `MEMORY_RECALL_*` 坏值会直接报错**:早期 topk/rrf/窗口等参数仍是直接 `int/float` 解析;
  Phase 2 新旋钮才做坏值回落。
- **自定义 provider base_url 配错**常见表现是 LLM 调用 HTTP 405;先查
  `~/.memory_system/custom_providers.json` 的 `base_url`。
- **本机代理坑**:跑 web/provider 相关测试前清代理:
  `export no_proxy=127.0.0.1,localhost` 并 `unset http_proxy https_proxy all_proxy`。
- **MCP stdout 是协议信道**:server 和下游 helper 不得向 stdout 打印日志;业务失败必须返回
  `content:[{type:"text",text:"..."}]`,不能用 JSON-RPC error 或裸异常杀掉进程。
- **选路描述有两层生效时机**:server 的 `tools/list` 每次现读;MCP 客户端可能缓存当前连接,
  因此控制台编辑一般到下一次会话才影响模型选路。

## 验证

后端回归(优先用项目 `.venv`,否则可能因包路径或 `sqlite_vec` 未装失败):

```bash
.venv/bin/python scripts/verify_s1.py
.venv/bin/python scripts/verify_s2.py
.venv/bin/python scripts/verify_s3.py
.venv/bin/python scripts/verify_s4.py
.venv/bin/python scripts/verify_s5.py
.venv/bin/python scripts/verify_delete.py
.venv/bin/python scripts/verify_edit.py
.venv/bin/python scripts/verify_s6.py
.venv/bin/python scripts/verify_mcp.py
.venv/bin/python scripts/verify_web_api.py
.venv/bin/python scripts/verify_view_api.py
.venv/bin/python scripts/verify_provider_config.py
```

检索质量评测(非通过门,调参用):

```bash
.venv/bin/python scripts/eval_recall.py
```

前端语法 + NUL 检查见 `history_notes/S5_NOTES.md §前端注意 / §验收命令`。

## 文档地图

当前依据:
- `ARCHITECTURE.md` — 当前架构正典:铁律、数据流、模块边界、schema/API、当前风险。
- `README.md` — 使用入口与阶段概览。
- `history_notes/S5_NOTES.md` — S5 写入侧历史、前端坑、验收门、旧工程债。
- `history_notes/S6_NOTES.md` — S6 检索层历史、评测基建、Phase 2 收口、MCP 前置。
- `history_notes/ENGINEERING_HISTORY.md` — 横跨 S5/S6 的工程整理时间线。
- `../project/idea_v2.md` — 概念正典。
- `../project/s6_build_plan.md` / `../project/s6_phase2_build.md` — S6 裁定与施工书。
- `../project/recall_tools_plan.md` / `../project/mcp_build_plan.md` — MCP recall tools 契约与施工依据。
- `MCP_BUILD_REPORT.md` — MCP 本轮改动、接口、验收结果与施工偏离记录。

历史参考,不要按它施工:
`../project/plan_v3.md`、`../project/out/plan_v1.md`、`../project/out/plan_v2.md`、`../project/out/idea.md`。
