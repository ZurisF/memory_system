# memory_system 接手笔记

> 目的:让后续实例(**S6 检索层**)快速看清真实状态、下一步、验证方式、高优先风险。
> - 架构全貌 → **`ARCHITECTURE.md`**(认知正典:分层/接口/铁律/数据流)。
> - S5 写入侧细节(语义/前端坑/验收门/工程债) → **`S5_NOTES.md`**。
> - 概念正典 → `project/idea_v2.md`。
> 本文件只写「现状 + 交接 + 下一步」,刻意保持清爽。最近整理:2026-06-25。

## 当前状态

- **S1–S5 引擎 / API / CLI / 前端全绿**。写入侧(切段 → 蒸馏 → 提取 → 审核入库)、查看侧只读
  galaxy、控制台(provider 配置/自定义/key 掩码/连接测试)、三视图导航,全部就绪。
- 记忆正本是 `fragments/` 碎片;`memory.db` 是可重建索引(vec0 向量 + FTS5 trigram + 概念图)。
  三条铁律(碎片是正本 / uuid 不上台面 / key 不落盘)见 `ARCHITECTURE.md §2`。
- **provider 知识已收口**到 `agent/registry.py`(单一来源,2026-06-25);server.py 随之瘦身。
- 四项工程债 + 边计算性能标记已修(2026-06-25),详见 `S5_NOTES.md`。
- 前端仍是零构建原生静态资源,各 JS 模块职责单一,可单独改。

## 下一步:S6 检索层

S6 做的是「查询 → 召回 → 排序 → 注入回 Claude Code」。**底座已就位,不用从零起**:

| 能力 | 现成的东西 | 位置 |
|---|---|---|
| 向量召回 | `episode_vectors`(vec0 `FLOAT[dim]`)+ embedding provider 工厂 + meta 锁校验 | `db/migrations/m002`、`embedding.get_provider`、`index.assert_embeddable` |
| 全文召回 | `episode_fts`(FTS5 trigram,触发器自动同步 source_text) | `m002` |
| 概念图扩展 | `episode_nodes` 膜(FK CASCADE)+ node 共现边(现算) | `views.py` |
| 只读读层 | `list_memories` / `read_memory` / `read_node_detail`(已剥 uuid/向量) | `views.py` |

**S6 要自己定的(待厘清)**:
- 查询入口形态:CLI? HTTP API? Claude Code hook 自动注入?
- 召回融合:向量 + FTS + 图扩展怎么合并/去重/打分。
- 排序信号:`salience_tier`(1–3)、新鲜度、衰减时钟 `last_accessed_at`(注意:这是**运行态**,
  `index rebuild` 会重置为 `activated_at`,非记忆正本——不能当检索质量的唯一依据)。
- 注入格式与 token 预算。
- 概念依据看 `project/idea_v2.md` 检索相关章节(召回/激活/衰减)。

## 高优先风险(S6 也要知道)

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
.venv/bin/python scripts/verify_web_api.py
.venv/bin/python scripts/verify_view_api.py
.venv/bin/python scripts/verify_provider_config.py
```
前端语法 + NUL 检查、浏览器烟测:见 `S5_NOTES.md §前端注意 / §验收命令`。

## 文档可信度

可作当前依据:
- `ARCHITECTURE.md` — 架构认知正典(分层/接口/铁律/数据流/schema/API)。**先读这份。**
- `S5_NOTES.md` — S5 写入侧语义、前端坑、验收门 + 2026-06-25 工程债与 registry 重构记录。
- `README.md` — 与当前代码接近。
- `project/idea_v2.md` — 概念正典。
- `project/frontend_plan.md` — 前端施工书,§7 API 契约、§8 数据对象形状。
- `project/prompts_extraction.md` — Prompt 2 基本可信;Prompt 1 早期文字写了行号,真实打包已改回合制。

历史参考,**不要按它施工**(含 `claude-memory`、`~/.claude-memory`、cron、FastAPI/HTMX、Ollama、
activation decay 等与当前方向冲突的旧设定):
`project/plan_v3.md`、`project/out/plan_v1.md`、`project/out/plan_v2.md`、`project/out/idea.md`。
