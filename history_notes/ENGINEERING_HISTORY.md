# 工程历史索引

> 用途:把曾经塞在 `ARCHITECTURE.md` 和 `HANDOFF_NOTES.md` 里的历史过程集中存放。
> 当前事实以 `../ARCHITECTURE.md` 和 `../HANDOFF_NOTES.md` 为准。

## 2026-06-25:provider 注册表与早期工程债

- SQLite 连接加 `busy_timeout=5000`,降低本地多线程 server/CLI 并发写时的 `SQLITE_BUSY`。
- 迁移器从 `MAX(version)` 改为读取实际 `schema_migrations` 行集合,避免缺中间版本时误判。
- fake embedding rebuild 不再写 meta 锁,避免测试库污染真实 DashScope 锁。
- `preview_cache.get()` 接上 `sweep_stale`,同一 jsonl 旧 mtime 缓存自动清理。
- `views.py` 给 node 共现边 O(E*K^2) 实时计算补性能标记。
- provider 知识收口到 `agent/registry.py`,内置 provider、自定义 provider、key 掩码、占位 key 和 info
  拼装有了单一来源。

## 2026-06-26:S5 写入侧补齐

- `server.py` 抽薄:env 写回、探活、UI shape 裁剪分别下沉到专门模块。
- 前端裸全局收进 `ST = {}` 命名空间,保留非 ES module 的零构建路线。
- 蒸馏提取支持线程池并发和逐条落盘;失败段内联 retry,可忽略关闭。
- 已入库 episode/node 支持真删;删 node 会从引用它的 episode 碎片摘除 label,避免 rebuild 复活。
- 已入库 episode 支持正文四件编辑;只有 overview 真变才重嵌。

## 2026-06-27:S5 前端编辑体验

- 蒸馏屏 nodes 从裸 JSON textarea 改为结构化行(label/action/alias/reason),后端契约保持不变。

## 2026-07-01:健壮性一轮

- 碎片、`.env`、`custom_providers.json`、预览缓存统一 tmp + `os.replace` 原子写。
- `segments_store` / `staging_store` 加进程内 RLock,保护 load -> edit -> write。
- `.env` reload 不再覆盖 shell export 的真 key。
- transcript 列表热路径不再默认数行,减少冷启动成本。
- rebuild 清空与重灌放进同一事务,失败整体回滚。
- 前端修属性逃逸、裸 fetch 兜底、在途锁、后发者胜等散点。

## 2026-07-02 到 2026-07-07:S6 检索层

详见 `S6_NOTES.md`:
- Phase 1:detail/episode/concept 三路检索、衰减、重构、开场注入。
- 评测基建:合成语料、ingest/reset、recall metrics、前端召回屏。
- Phase 2:session 去重/冷却、alias_bridges、opening spark、recall provider。

## 当前仍保留的非债/待定项

- confirm/edit 的 DB commit 与碎片写回之间有崩溃窗口,这是刻意选择的落地顺序。
- node 共现边暂不物化,等数据量证明价值。
- 概念图编辑、孤儿 episode 可见化、SessionStart hook、MCP tools 仍是后续工作。
