# memory_system

Claude Code 持久化记忆系统。**架构总览见 `ARCHITECTURE.md`**(分层/接口/铁律/数据流,先读这份);
概念正本见 `../project/idea_v2.md`,交接与下一步见 `HANDOFF_NOTES.md`,历史施工笔记见 `history_notes/`。

三层:**原文(source_text)→ 情景(episode)→ 语义(nodes)**。人驱动入库,惰性衰减检索,碎片是正本、SQLite 是可重建索引。

## 开发

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

embedding 走 DashScope `text-embedding-v4`(1024 维),key **只从环境读、不落代码/库**。两种给法:

```bash
# 1) 临时 export
export DASHSCOPE_API_KEY=sk-...

# 2) 推荐:写进数据主目录的 .env(仓库之外,自动加载)
#    memory-system init 会生成 ~/.memory_system/.env.example,复制为 .env 填 key 即可
DASHSCOPE_API_KEY=sk-...
```

已 export 的环境变量优先于 `.env`。`memory-system doctor` 会报告 key 有无(打码,不泄明文)。

## CLI

```bash
memory-system init                 # 建数据主目录(幂等)
memory-system migrate status       # schema 版本
memory-system migrate up           # 应用迁移
memory-system migrate down         # 回滚一步
memory-system doctor               # 健康检查
memory-system diagnose claude-code # 实测平台 transcript 形态,落报告
memory-system embed "一段文本"     # 实测 embedding(--provider fake|dashscope)
memory-system index rebuild        # 从碎片全量重建 DB+向量+FTS(--provider fake 免联网)
memory-system scan                 # 列出 transcript(CLI 视角)
memory-system serve                # 启本地审核前端 → http://127.0.0.1:8765

# 入库流水(选段 → 切块 → 提取 → 审核归档)
memory-system chunk <jsonl>            # 切块(S3):调 agent 建议分段,落工作文件(--manual 1-8,9-20 手动)
memory-system extract <jsonl>          # 提取(S4):对确认的段逐段出五件套,落 staging(--seg s1,s3)
memory-system confirm <jsonl> --stage e1   # 审核(S5):确认 staging→active 碎片 + 增量入库(--all 全确认)
memory-system reject <jsonl> --stage e1    # 审核(S5):拒一条 staging(留痕,不入库)
memory-system archive ep_a1b2c3d4          # 审核(S5):active 碎片降级为 archived

# 检索(S6)
memory-system recall detail "逐字找原文"        # FTS grep + 开窗(--raw 整条原文,--since/--until)
memory-system recall episode "那次讨论衰减"     # 双路召回+三槽,默认重构成自然语言(--raw/--json 不调 LLM)
memory-system recall concept "记忆系统"         # 概念全量取(--context "..." 按语境排序)
memory-system opening rebuild                   # 重建开场注入 cache(默认仅 dirty 时;--force 强制)
memory-system opening show                      # 读开场 cache(SessionStart hook 读的就是这个文件)
memory-system mcp                               # 启动 stdio MCP recall tools(stdout 仅走 JSON-RPC)
```

数据主目录默认 `~/.memory_system`,可用环境变量 `MEMORY_SYSTEM_HOME` 改。

> `index rebuild` 用真 provider(dashscope)会对所有 episode overview 联网重嵌、耗额度;
> 测试用 `--provider fake`。

## MCP recall tools

`memory-system mcp` 提供三个独立 tool,检索语义全部复用现有 S6 引擎:

- `memory_recall_episode`:只有模糊印象或拿不准选路时,回忆相关情景;默认重构成自然语言。
- `memory_recall_detail`:按字面词句逐字找原文,支持时间窗和整条原文。
- `memory_recall_concept`:按 node/别名调取某人、项目或概念下的全部 active 记忆。

注册上线仍以 Phase A 召回达标门通过并由 zuris 显式拍板为前提,代码验收通过不等于默认放行。
获批后可按用户级注册:

```bash
claude mcp add --scope user memory -- <repo>/.venv/bin/memory-system mcp
```

三份选路描述可在控制台「过程 Prompt」的「选路」分组编辑。server 每次 `tools/list` 都现读文件,
但 MCP 客户端通常按连接缓存 tool 列表,编辑后的描述一般从下一次会话生效。

SessionStart 开场回忆与 MCP 独立。要在用户级 `~/.claude/settings.json` 接线时,可加入下面的一行
hook 配置;它只读仓库外数据主目录中的预生成缓存,不现算、不调 LLM:

```json
{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"cat \"${MEMORY_SYSTEM_HOME:-$HOME/.memory_system}/opening_cache/global.md\" 2>/dev/null || true"}]}]}}
```

## 前端

`memory-system serve` 起的是**零依赖**本地前端(标准库 http.server + 原生 HTML/JS),**四视图单页**
(写入 | 查看 | 召回 | 控制台,切换=显隐冻结):

- **写入侧**:列 transcript(已自动隐藏 `/clear` 空壳等空会话)、清洗预览、选回合标「已处理」、
  切块(运行 agent / 并分移边界 / 手动建段 / 标删 / 保存),以及「蒸馏」审核:按父 jsonl 聚类、
  段预览、五件套编辑、提取、确认/拒绝/删除、批量操作。
- **查看侧**:galaxy 力导向图(只读)显示已入库记忆,点节点看详情、点条目看五件套;node↔node 边 =
  共享 episode 的共现。
- **召回侧**:detail / episode / concept 三路检索,左侧看结构化槽位,右侧可让 recall agent 重构自然语言回忆。
- **控制台**:agent provider 配置/切换/保存(切块/提取/重构三角色模型)、自定义 OpenAI 兼容
  provider 增删、key 密文掩码、连接测试(chat + embedding)、切块/提取/重构/选路 prompt 在线编辑
  (八键白名单;选路描述受 MCP 客户端连接级缓存边界约束)。

前端文件在 `memory_system/web/`,仍是零构建静态资源;S5 细节见 `history_notes/S5_NOTES.md`。

## 阶段

当前:**S0–S6 + S6 Phase 2 + MCP recall tools 已落地**——写入侧、查看侧、召回屏、控制台、
三路检索 `recall detail/episode/concept`、重构 agent、惰性衰减、开场注入、session 去重/冷却、
别名锚定、开场 spark、recall 专用 provider 通道和 stdio MCP 薄封装均已就绪。MCP 尚未注册上线,
仍需先过 Phase A 召回达标门并由 zuris 显式拍板。

下一步候选:跑 Phase A 达标门并决定是否注册 MCP、概念图编辑、按需在仓库外接 SessionStart hook。
详见 `HANDOFF_NOTES.md`。
