# memory_system

Claude Code 持久化记忆系统。**架构总览见 `ARCHITECTURE.md`**(分层/接口/铁律/数据流,先读这份);
概念正本见 `../project/idea_v2.md`,施工脊梁见 `../project/phase1_build.md`,交接与下一步见 `HANDOFF_NOTES.md`。

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
```

数据主目录默认 `~/.memory_system`,可用环境变量 `MEMORY_SYSTEM_HOME` 改。

> `index rebuild` 用真 provider(dashscope)会对所有 episode overview 联网重嵌、耗额度;
> 测试用 `--provider fake`。

## 前端

`memory-system serve` 起的是**零依赖**本地前端(标准库 http.server + 原生 HTML/JS),**三视图单页**
(写入 | 查看 | 控制台,切换=显隐冻结):

- **写入侧**:列 transcript(已自动隐藏 `/clear` 空壳等空会话)、清洗预览、选回合标「已处理」、
  切块(运行 agent / 并分移边界 / 手动建段 / 标删 / 保存),以及「蒸馏」审核:按父 jsonl 聚类、
  段预览、五件套编辑、提取、确认/拒绝/删除、批量操作。
- **查看侧**:galaxy 力导向图(只读)显示已入库记忆,点节点看详情、点条目看五件套;node↔node 边 =
  共享 episode 的共现。
- **控制台**:agent provider 配置/切换/保存、自定义 OpenAI 兼容 provider 增删、key 密文掩码、
  连接测试(chat + embedding)。

前端文件在 `memory_system/web/`,仍是零构建静态资源;S5 细节见 `S5_NOTES.md`。

## 阶段

当前:**Phase 1 / S0–S5 全绿**——引擎 + 写入侧富 GUI + 查看侧只读 galaxy + 控制台 + 三视图导航全部就绪
(`verify_s1`~`verify_s5` + `verify_s6` + `verify_web_api` + `verify_view_api` + `verify_provider_config` 全过)。
下一步:**S6 检索层**(向量召回 + FTS + 图扩展);编辑写回降到 Phase 2。详见 `HANDOFF_NOTES.md`。
逐步通过门见 `phase1_build.md`。
