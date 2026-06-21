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

## 前端(GUI 至 S3 切块;S5 富审核界面待建)

`memory-system serve` 起的是**零依赖**本地前端(标准库 http.server + 原生 HTML/JS)。
当前 GUI:列 transcript(已自动隐藏 `/clear` 空壳等空会话)、清洗预览、选回合标「已处理」、
切块(运行 agent / 并分移边界 / 手动建段 / 标删 / 保存)。S4 提取与 S5 审核归档当前走 CLI/API,
**S5 富审核界面(按父 jsonl 聚类、五件套编辑、单开去噪对比、批量归档)是下一步前端工作**。

## 阶段

当前:**Phase 1 / S0–S5 引擎全绿**(S5 第一段「入库闭环」:staging→active 碎片 + 增量入库 + node 别名
合并,`verify_s1`~`verify_s5` 全过)。下一步:S5 富审核 GUI,然后 S6 检索模块。
逐步通过门见 `phase1_build.md`。
