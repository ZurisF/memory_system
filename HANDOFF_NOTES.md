# memory_system 接手笔记

> 目的:让后续实例快速看清当前真实状态、下一步、验证方式和少数高优先风险。
> 最近整理:2026-06-25。S5 Phase 1 已收尾；四项工程债已修。

## 当前状态

- S1–S5 引擎/API/CLI 当前全绿;用项目 `.venv` 跑 `scripts/verify_s1.py` 到 `verify_s5.py` 以及 `verify_web_api.py`、`verify_view_api.py`。
- **S5 Phase 1 收尾(2026-06-25)**:
  - **写入侧**:全流水线（切段 / 蒸馏 / extract / 五件套就地编辑 / confirm / reject / delete）+ 段预览 + LLM 在途锁 + 轻量撤销。
  - **查看侧**:galaxy 只读显示（`view.js` + `views.py`），接 `/api/memories` 真数据；node↔node 共现 edges 含 `via` 共享情景。
  - **控制台**:agent config 显示/切换/保存、自定义 provider 增删、key 密文掩码、连接测试（chat + embedding）全部就绪。
  - **三视图导航**:写入 | 查看 | 控制台，切换 = 显隐冻结(不销毁)。
- **node↔node 边 = 共享 episode 共现**（idea_v2 §17/§96 正典，非缺失），后端按 `episode_nodes` 算、含 `via` 共享情景。独立 `edges` 表是"可选物化缓存"未建、入口留。当前 O(E·K²) 实时计算，`views.py` 行 85 有 NOTICE 标注。
- **四项工程债已修(2026-06-25)**:
  - SQLite `busy_timeout`：`connection.py` 已加 `PRAGMA busy_timeout = 5000`，并发写不再炸 `SQLITE_BUSY`。
  - 迁移器 `MAX(version)` 误判：新增 `applied_versions()` 读实际行集合，`status`/`up` 不再靠 max 推断缺口。
  - Fake provider 写锁：`assert_embeddable` 加 `lock_meta` 参数，`index rebuild --provider fake` 不再落 meta 锁，对齐 `cmd_init` 行为。
  - `preview_cache.sweep_stale`：已在 `get()` 创建新缓存后接线，自动清旧 mtime 缓存文件。
- 前端仍是零构建原生静态资源。各 JS 模块职责单一，可单独改。

### Phase 1 完成度

| 模块 | 关键文件 | 状态 |
|---|---|---|
| 写入侧全流水线 | `chunk.js`, `triage.js`, `server.py` | ✅ |
| 切段 ops（加回合到段、向上合并等） | `chunk.js` | ✅ |
| 查看侧 galaxy 只读 | `view.js`, `views.py` | ✅ |
| 控制台（配置/切换/key掩码/测试/自定义provider） | `console.js`, `server.py` | ✅ |
| 全部读口 API | `server.py` + `views.py` | ✅ |
| 三视图导航 + 冻结 | `view.js`, `index.html`, `styles.css` | ✅ |
| LLM 在途锁 / 处理锁 | `api.js`, `transcripts.js` | ✅ |
| 错误告警可关闭、base_url 校验 | `chunk.js`, `server.py` | ✅ |
| 工程债 1-4（busy_timeout / 迁移器 / fake锁 / sweep_stale） | `connection.py`, `migrate.py`, `index.py`, `preview_cache.py` | ✅ |

### 推迟到 Phase 2 / 下一轮

| 项目 | 说明 |
|---|---|
| 编辑写回 | `editor.py` + `POST /api/memory/edit` + `POST /api/node/edit`（改 overview 须重嵌向量）；galaxy 面板编辑按钮 |
| 自动精炼 agent | 第 4 个 agent + diff 红绿块 + refine→extract 重排流水线 |
| 段拖拽排序 | 语义冲突（段按回合序），需先理清 |
| ctrl-z 多步撤销 | 当前只支持"还原到上次保存"（单步） |
| 物化 edges 缓存表 | 等证明价值后再建，`views.py` 入口已留 NOTICE |

### 代码地图（关键文件）

前端 (`memory_system/web/`):
- `index.html` — 页面骨架与脚本顺序
- `styles.css` — 样式
- `state.js` — 全局状态、localStorage、通用工具、`TPREVIEW` 预览缓存
- `transcripts.js` — transcript 列表、候选篮子、阶段切换、`beginEdit`/`markDirty`
- `chunk.js` — 切段屏逻辑、`showAlert`/`renderAlerts`、`runChunk`、段操作
- `triage.js` — 蒸馏/审核屏逻辑、段预览、五件套编辑、批量 confirm/reject/delete
- `api.js` — provider 加载(按 role 选默认)、`postJSON`、`once(key, fn, btn)` 在途锁
- `app.js` — 启动与事件绑定
- `view.js` — 三视图导航(显隐冻结) + galaxy 力导向图(只读)
- `console.js` — 控制台(agent 配置 + 自定义 provider + key 掩码 + 连接测试)

后端关键文件:
- `memory_system/server.py` — HTTP API 路由 + handler(全部 GET/POST/DELETE)
- `memory_system/views.py` — 查看侧只读查询 + 边共现计算(NOTICE: O(E·K²),未来物化)
- `memory_system/index.py` — DB 增量同步 + 向量嵌入 + `assert_embeddable`(lock_meta 参数)
- `memory_system/archive.py` — confirm/reject/archive 引擎
- `memory_system/fragments.py` — 碎片正本读写
- `memory_system/db/connection.py` — SQLite 连接(busy_timeout=5000 + WAL + vec0)
- `memory_system/db/migrate.py` — 迁移器(`applied_versions` 读实际行集,非 MAX 推断)
- `memory_system/preview_cache.py` — 预览缓存(`get` 自动 `sweep_stale` 清旧文件)
- `memory_system/cli.py` — CLI 入口(`cmd_index` fake provider 不落 meta 锁)
- `memory_system/staging_store.py` — staging 工作态读写 + `edit_episode`(白名单 `_EDITABLE`)
- `memory_system/segments_store.py` — chunks 工作态读写 + uuid 重算
- `memory_system/agent/` — provider 工厂 + claude_cli / openai_compat / fake
- `memory_system/embedding/` — embedding provider 工厂 + dashscope / fake

## 下一步

1. **Phase 2**:自动精炼 agent + diff 红绿块 + refine→extract 重排流水线 + 段拖拽排序（需先厘清语义）。
2. **编辑写回（可选）**:`editor.py` + `POST /api/memory/edit` + `POST /api/node/edit`；galaxy 面板编辑按钮接线。
3. **剩余低优先**:
   - 自定义 provider base_url 配错：控制台已有校验提示但非强制拦截。
   - `/api/transcripts` 冷缓存首次列表会 clean 全部 jsonl,大库下可能慢。
   - `index rebuild` 对所有 episode overview 全量重嵌;真 DashScope 会联网、耗时、耗额度。
   - 物化 edges 缓存表（等数据量证明价值）。

## 验证

常规回归:
```bash
.venv/bin/python scripts/verify_s1.py
.venv/bin/python scripts/verify_s2.py
.venv/bin/python scripts/verify_s3.py
.venv/bin/python scripts/verify_s4.py
.venv/bin/python scripts/verify_s5.py
.venv/bin/python scripts/verify_web_api.py
.venv/bin/python scripts/verify_view_api.py
```

前端语法:
```bash
node --check memory_system/web/state.js
node --check memory_system/web/transcripts.js
node --check memory_system/web/triage.js
node --check memory_system/web/api.js
node --check memory_system/web/chunk.js
node --check memory_system/web/app.js
node --check memory_system/web/view.js
node --check memory_system/web/console.js
python3 -m py_compile memory_system/server.py memory_system/views.py
# NUL 字节检查:
python3 -c "
from pathlib import Path
for p in Path('memory_system/web').glob('*.js'):
    n = p.read_bytes().count(b'\x00')
    print(f'{p}: NUL={n}')
    assert n == 0
"
```

注意:
- 直接用系统 `python3 scripts/verify_s*.py` 可能因包路径或 `sqlite_vec` 未安装失败。优先用 `.venv/bin/python ...`。
- 在受限 sandbox 内直接绑定本地端口可能被拒;HTTP smoke test 如需临时绑定 `127.0.0.1`,允许后再跑。
- `verify_web_api.py` 会临时起 `ThreadingHTTPServer`,覆盖 staging edit/reject/confirm 和删段 HTTP 语义。

浏览器烟测:
- `memory-system serve` 后硬刷新页面。
- 切段:打开 transcript、手动建段、保存、删段、跑切块 agent。
- 蒸馏:段预览、提取、编辑五件套、确认/拒绝/删除、连点时在途锁生效。
- 查看:galaxy 力导向图显示已入库记忆、点节点看详情、点条目看五件套。
- 控制台:切换 provider/model → 保存 → 连接测试通过；添加自定义 provider 时 base_url 有校验提示。

## 高优先风险

- **自定义 provider base_url 配错**:若 URL 不是 API 端点（如写了 Web 平台域名），LLM 调用会报 HTTP 405。控制台添加 provider 时已有校验提示（缺 `/v1` 尾缀、常见平台域名误用），但非强制拦截。出问题时先查 `~/.memory_system/custom_providers.json` 的 `base_url`。
- **迁移器旧库坏状态**:迁移器已改为读实际行集合 (`applied_versions`)，但若旧库已有坏状态（如 `schema_migrations` 缺少中间版本），需手动补。
- **`/api/transcripts` 冷缓存首次列表会 clean 全部 jsonl**,大库下可能慢。
- **`index rebuild` 对所有 episode overview 全量重嵌**;真 DashScope 会联网、耗时、耗额度。

## 文档可信度

可作为当前依据:
- `README.md` — 与当前代码接近。
- `S5_NOTES.md` — S5 写入侧语义、前端坑位、验收门 + 2026-06-25 工程债修复记录。
- `project/idea_v2.md` — 当前概念正本。
- `project/phase1_build.md` — 当前施工脊梁,但部分早期表述与落地实现不同。
- `project/prompts_extraction.md` — Prompt 2 基本可信;Prompt 1 早期文字仍写行号,真实打包 prompt 已改回合制。
- `project/s3_chunking_plan.md` — S3 方案基本可信,但早期 `line_map` / `L-range` 描述已过时。
- `project/frontend_plan.md` — 前端施工书,§7 API 契约大全、§8 数据对象形状——写前端第一参考。

历史参考,不要按它施工:
- `project/plan_v3.md`
- `project/out/plan_v1.md`
- `project/out/plan_v2.md`
- `project/out/idea.md`

这些旧文档包含 `claude-memory`、`~/.claude-memory`、cron、FastAPI/HTMX、Ollama、activation decay 等旧设定,与当前方向冲突。
