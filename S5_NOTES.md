# S5 注意事项

> 用途:沉淀 S5 写入侧的长期语义、前端坑位和验收门。主交接只写下一步;S5 细节来这里查。
> 当前状态:2026-06-24 写入侧 Phase 1 已收尾,下一步转查看侧 demo、控制台、三视图导航骨架。

## 当前状态

- S5 引擎/API/CLI 已完成:`confirm/reject/archive`、staging 编辑、active episode 碎片 + 增量 DB 入库。
- S5 GUI 写入侧已完成主链路:`切段 -> 蒸馏 -> extract -> staging edit -> confirm/reject/delete`。
- 前端仍是零构建原生静态资源,位于 `memory_system/web/`:
  - `index.html`:页面骨架与脚本顺序。
  - `styles.css`:样式。
  - `state.js`:全局状态、localStorage、通用工具、`TPREVIEW` 预览缓存。
  - `transcripts.js`:左侧 transcript 列表、候选区、切段/蒸馏阶段切换。
  - `chunk.js`:切段屏逻辑。
  - `triage.js`:蒸馏/审核屏逻辑、段预览、五件套编辑。
  - `api.js`:provider 加载、`postJSON`、`once(key, fn, btn)` 在途锁。
  - `app.js`:启动与事件绑定。

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

## 后续 S5 方向

- 查看侧 demo API:`/api/memories`、`/api/memory`、`/api/node`、`/api/memory/edit`、`/api/node/edit`。
- 控制台 API/UI:`/api/agent/config`、可选 `/api/agent/test`;key 只显示掩码,不进前端表单。
- 顶部导航 + 三视图骨架:写入 / 查看 / 控制台。
- 单开去噪对比、自动精炼 agent、galaxy 可视化留后续阶段。
