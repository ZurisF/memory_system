# memory_system

Claude Code 持久化记忆系统。概念正本见 `../project/idea_v2.md`,施工脊梁见 `../project/phase1_build.md`。

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
memory-system diagnose claude-code # 实测平台 transcript / resume 信号,落报告
memory-system embed "一段文本"     # 实测 embedding(--provider fake|dashscope)
```

数据主目录默认 `~/.memory_system`,可用环境变量 `MEMORY_SYSTEM_HOME` 改。

## 阶段

当前:**Phase 1 / S0 地基 + 平台实测**。逐步通过门见 `phase1_build.md`。
