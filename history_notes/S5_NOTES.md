# S5 历史笔记

> 用途:归档 S5 写入侧的长期语义、前端坑位、验收门和当时的工程债修复。
> 当前架构事实看 `../ARCHITECTURE.md`;当前交接看 `../HANDOFF_NOTES.md`。
> 本文件保留历史语境,其中「三视图」「下一步」等说法按当时状态理解。

## 当前状态

### Phase 1 完成度

| 模块 | 关键文件 | 状态 |
|---|---|---|
| 写入侧全流水线 | `chunk.js`, `triage.js`, `server.py` | ✅ |
| 切段 ops(加回合到段、向上合并等) | `chunk.js` | ✅ |
| 查看侧 galaxy 只读 | `view.js`, `views.py` | ✅ |
| 控制台(配置/切换/key掩码/测试/自定义provider) | `console.js`, `server.py` | ✅ |
| 全部读口 API | `server.py` + `views.py` | ✅ |
| 三视图导航 + 冻结 | `view.js`, `index.html`, `styles.css` | ✅ |
| LLM 在途锁 / 处理锁 | `api.js`, `transcripts.js` | ✅ |
| 错误告警可关闭、base_url 校验 | `chunk.js`, `server.py` | ✅ |


- S5 引擎/API/CLI 已完成:`confirm/reject/archive`、staging 编辑、active episode 碎片 + 增量 DB 入库。
- S5 GUI 写入侧已完成主链路:`切段 -> 蒸馏 -> extract -> staging edit -> confirm/reject/delete`。
- 前端仍是零构建原生静态资源,位于 `memory_system/web/`:
  - `index.html`:页面骨架与脚本顺序。
  - `styles.css`:样式。
  - `state.js`:全局状态、localStorage、通用工具、`TPREVIEW` 预览缓存。
  - `transcripts.js`:左侧 transcript 列表、候选区、切段/蒸馏阶段切换。
  - `chunk.js`:切段屏逻辑、`showAlert`/`renderAlerts`、段操作。
  - `triage.js`:蒸馏/审核屏逻辑、段预览、五件套编辑。
  - `api.js`:provider 加载(按 role 选默认)、`postJSON`、`once(key, fn, btn)` 在途锁。
  - `app.js`:启动与事件绑定。
  - `view.js`:三视图导航 + galaxy 力导向图(只读)。
  - `console.js`:控制台(agent 配置/切换/自定义 provider/key 掩码/连接测试)。
- 后端新增:
  - `views.py`:查看侧只读查询(`list_memories`/`read_memory`/`read_node_detail`)。

## 删除语义

- **段(segment) 删除**:从 `staging/chunks/<session>.json` 真删。删光后 chunks 工作文件 unlink,等价退出分段流水线。
- **episode reject**:打回重做。撤 staging episode,段回「未提取」,并留下 rejected 痕迹。
- **episode delete**:干净撤掉 staging episode,不留 rejected 痕迹,不入库,不碰段;段回「未提取」。
- **已提取段删除**:不静默删。`POST /api/segments/delete` 未带 `force` 时返回 409 `needs_confirm`;前端二次确认后带 `force:true` 重发。已提取 episode 不受影响。
- 删除只动工作态 chunks/staging,绝不碰已入库正本碎片或 DB。

相关回归:

- `scripts/verify_s5.py` 门 I:引擎层断言删段不碰 staging、删光 unlink、`remove_episode` 干净撤不留痕、不动 DB。
- `scripts/verify_web_api.py` 删段门:HTTP 层断言无 episode 段干净删、有 episode 不 force 回 409、force 后 episode 不受影响、`/api/staging/delete` 干净撤。

## 前端注意

- 面向用户文案是「切段 / 蒸馏」;内部 stage id 仍是 `chunk` / `triage`,不要为了文案改内部状态名。
- `triage.js` 的选中 key 使用 `"\u0000"` 字面量分隔。当前文件里不应有真实 NUL 字节;改相关行后必须检查。
- `once(key, fn, btn)` 用于 LLM/写库耗时操作防连点,已套在提取、批量提取、确认入库。切段 `runChunk` 自己会置灰按钮。
- 五件套编辑器已经移除 ctrl-z 全局劫持;文本框保留浏览器原生逐字撤销。按钮文案是「还原到上次保存」。
- 段预览只读源 transcript,用 `TPREVIEW` 按 path 缓存;源 jsonl 已清时预览按钮灰掉。

改前端后建议跑:

```bash
node --check memory_system/web/state.js
node --check memory_system/web/transcripts.js
node --check memory_system/web/triage.js
node --check memory_system/web/api.js
node --check memory_system/web/chunk.js
node --check memory_system/web/app.js
python3 - <<'PY'
from pathlib import Path
for p in Path("memory_system/web").glob("*.js"):
    n = p.read_bytes().count(b"\x00")
    print(f"{p}: NUL={n}")
    assert n == 0
PY
```

## 验收命令

常规回归:

```bash
.venv/bin/python scripts/verify_s1.py
.venv/bin/python scripts/verify_s2.py
.venv/bin/python scripts/verify_s3.py
.venv/bin/python scripts/verify_s4.py
.venv/bin/python scripts/verify_s5.py
.venv/bin/python scripts/verify_web_api.py
```

浏览器烟测:

- `memory-system serve` 后硬刷新页面。
- 切段:打开 transcript、手动建段、保存、删段。
- 蒸馏:段预览、提取、编辑五件套、确认/拒绝/删除、连点时在途锁生效。

## 2026-06-25 工程债修复

四项修复 + 一处性能标记 + 一次 provider 重构，全部后端，零前端波及。详见下方。S1–S5 引擎验证 +
provider 回归 + JS 语法 + NUL 检查全绿。

### 1. SQLite `busy_timeout` — `db/connection.py:19`

- **问题**:`connect()` 未设 `busy_timeout`。`server.py` 使用 `ThreadingHTTPServer`（每请求一个线程），GUI/CLI 并发写时偶发 `SQLITE_BUSY`（错误码 5），直接 500。
- **修复**:加 `PRAGMA busy_timeout = 5000`（等待 5 秒而非立即失败）。

### 2. 迁移器 `MAX(version)` 误判 — `db/migrate.py:50-87`

- **问题**:`current_version()` 用 `SELECT MAX(version)` 判断当前版本；若 `schema_migrations` 里只有 `{version:1}` 和 `{version:3}`（002 那行丢了），`MAX` 返回 3，`status()`/`up()` 误判 002 已应用，跳过建核心表（episodes/nodes/episode_nodes/episode_vectors/episode_fts 全在 `m002_core.py`）。
- **修复**:新增 `applied_versions()` → `SELECT version FROM schema_migrations` 返回实际行 `set[int]`；`status()` 和 `up()` 改为 `version in applied` 精确判断。顺手删了 `down()` 中未使用的 `cur = current_version(con)` 死代码。

### 3. Fake provider 写锁 — `index.py:50-63,227-228` + `cli.py:439`

- **问题**:`cmd_init` 在 `cfg.embedding.provider != "fake"` 时才写 meta 锁，但 `cmd_index rebuild` 没有同样的守卫。`index rebuild --provider fake` 会经 `assert_embeddable` 把 `model="fake"` 写入 meta 表，之后 DashScope 写入被永久拒绝（`provider.model != locked_model` → `ValueError`）。
- **修复**:`assert_embeddable` 加 `lock_meta: bool = True` 参数；`lock_meta=False` 且锁缺失时跳过写锁（仅返回 provider 的 model/dim 供本次会话使用）。`rebuild` 透传；`cmd_index` 在 `emb_cfg.provider != "fake"` 时才传 `lock_meta=True`，对齐 `cmd_init` 行为。`archive.confirm_episode` 调用 `assert_embeddable` 时使用默认 `lock_meta=True`（不受影响）。

### 4. `preview_cache.sweep_stale` 未接线 — `preview_cache.py:37`

- **问题**:`sweep_stale` 函数定义好了（遍历某 jsonl 的旧 mtime 缓存文件，只留最新的，其余 unlink），但整个代码库没有任何调用方。transcript 文件每次 mtime 变化都经 `get()` 生成新缓存文件，`cache/jsonl_preview/` 无限增长。
- **修复**:`get()` 在 cache miss → `clean()` → 写新缓存文件后，调 `sweep_stale(cache_dir, path, mtime)`，自动清掉同 jsonl 的旧 mtime 缓存。

### 5. 边计算性能标记 — `views.py:85-90`

- **问题**:当前共现边每次 `/api/memories` 请求都 O(E·K²) 实时计算（`itertools.combinations` 对所有 episode 的 node 两两配对）。非 bug，设计取舍——当前数据量无需物化表。
- **处理**:在共现边计算处加了 `NOTICE` 注释，标注复杂度和未来 `edges` 缓存表方向。API 形状不变，以后改读缓存表即可。

### 6. provider 知识收口到注册表 — 新增 `agent/registry.py`

- **问题**:provider 知识散在 4 处且重复——`config.py`(AgentConfig 默认 + 硬编码 deepseek base_url)、
  `agent/__init__.py` 工厂(再硬编码一份内置目录)、`server.py`(`_AGENT_PROVIDERS` 第二份 id 清单 +
  持久化 + 掩码 + info 拼装 + 增删改 + 测试)、`config.py:_load_custom_providers_map` 与
  `server.py:_load_custom_providers` 两份不同形状的 custom 加载。`[this is your api key]` 占位串写了 3 处。
- **修复**:新建 `agent/registry.py` 作单一来源——`BUILTINS` 目录(id/显示名/kind/默认 base_url/key_env)、
  `load_custom`/`save_custom`/`custom_map`、`all_provider_ids`/`providers_info`/`agent_key_status`、
  `mask_key`/`PLACEHOLDER_KEY`。工厂按 `kind` 分派;config / server 都从 registry 读。
  `_api_agent_test` 由「按 id 一长串 if/elif 各自建 provider」压成「过工厂 + 报 available()」。
  server.py 1092 → 970 行。`scripts/verify_provider_config.py` 的 import 跟着指向 registry。
- **循环依赖注意**:registry 顶层只 import 标准库、对 Config 走鸭子类型;`config.load_config` 与工厂内部
  对 registry 都做**函数内 import**。详见 `ARCHITECTURE.md §5.7`。

## 后续方向

- **编辑写回(Phase 1 最后一公里)** :`editor.py` + `POST /api/memory/edit` + `POST /api/node/edit`;galaxy 面板编辑按钮接上(改 overview 须重嵌向量)。
- **Phase 2**:自动精炼 agent + diff 红绿块、段拖拽排序（需先厘清语义）。
- 单开去噪对比、物化 edges 缓存表留后续阶段。
