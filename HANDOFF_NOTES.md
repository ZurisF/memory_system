# memory_system 接手笔记

> 目的:让后续实例快速看清当前真实状态、下一步、验证方式和少数高优先风险。
> 最近整理:2026-06-24。S5 写入侧专题细节已拆到 `S5_NOTES.md`,这里不再重复历史交接。

## 先读结论

- S1-S5 引擎/API/CLI 当前全绿;用项目 `.venv` 跑 `scripts/verify_s1.py` 到 `verify_s5.py` 以及 `verify_web_api.py`、`verify_view_api.py`。
- **查看侧读侧已落地(2026-06-24)**:新增 `views.py`(只读查询)+ `GET /api/memories`(含共现 edges)`/api/memory`/`/api/node`;前端加顶部三视图导航(写入|查看|控制台,切换=显隐冻结)+ `web/view.js` galaxy(移植 `project/front_references/browse.html`,接真数据**只读**)。回归 `verify_view_api.py`。
  - **node↔node 边 = 共享 episode 共现**(idea_v2 §17/§96 正典,非缺失),后端按 `episode_nodes` 算、含 `via` 共享情景;独立 `edges` 表是"可选物化缓存"未建、入口留。frontend_plan.md §5/§7.2/§9 旧表述"边不存在"已更正。
  - **zuris 关注点(待办)**:数据库建边的现行做法他有意见,暂搁置,正事做完再议。
  - 本轮**只读**:面板编辑写回、`editor.py`/`POST /api/memory/edit`/`/api/node/edit` 留下一轮(和前端编辑反馈一起)。控制台仍是占位壳。
- 两个早期 P1 已修并有回归:
  - confirm 失败残留 node 碎片:已改为 DB commit 成功后才落 node/episode 碎片,见 `verify_s5` 门 H。
  - agent 切块坏边界静默夹紧:已改为越界/逆序/非整数抛错走重试,见 `verify_s3` 门 7。
- S5 写入侧 Phase 1 已收尾:「切段 / 蒸馏」两屏、段预览、五件套编辑、extract/confirm/reject/delete、删除回归门、LLM 在途锁、轻量撤销修都已完成。
- 前端仍是零构建原生静态资源,但已从巨型 `app.js` + 内联 CSS 拆分。后续可以单独改对应文件。
- 下一步主线:查看侧**编辑写回**(`editor.py` + `/api/memory/edit`、`/api/node/edit`,改 overview 须重嵌向量)+ 控制台(`/api/agent/config`,key 只掩码)。S5 细节先看 `S5_NOTES.md`。

## 当前代码地图

- 包名/命令:`memory_system` / `memory-system`。
- 数据主目录默认 `~/.memory_system`,可由 `MEMORY_SYSTEM_HOME` 覆盖。
- GUI:`memory-system serve`,标准库 `http.server`,默认 `http://127.0.0.1:8765`。
- embedding 默认 DashScope `text-embedding-v4`,1024 维;fake provider 只用于离线测试。
- chat agent 层已有 `claude_cli` / OpenAI 兼容 / fake。切块默认 `sonnet`,提取默认 `opus`。

前端文件:

- `memory_system/web/index.html`:页面骨架与脚本顺序。
- `memory_system/web/styles.css`:样式。
- `memory_system/web/state.js`:全局状态、localStorage、通用工具、`TPREVIEW` 预览缓存。
- `memory_system/web/transcripts.js`:transcript 列表、候选篮子、阶段切换。
- `memory_system/web/chunk.js`:切段屏逻辑。
- `memory_system/web/triage.js`:蒸馏/审核屏逻辑、段预览、五件套编辑。
- `memory_system/web/api.js`:provider 加载、`postJSON`、`once(key, fn, btn)` 在途锁。
- `memory_system/web/app.js`:启动与事件绑定。

阶段状态:

- S1:碎片正本、DB 重建、FTS、向量。
- S2:transcript 发现、清洗预览、段级 processed 标记、空壳过滤。
- S3:切块 agent + GUI,段工作态落 `home/staging/chunks/<session>.json`。
- S4:提取五件套,按块回滚,staging 工作态落 `home/staging/episodes/<session>.json`。
- S5:confirm/reject/archive 引擎/API/CLI + 写入侧 GUI 已可用。专题注意事项见 `S5_NOTES.md`。

## 下一步

✅ 已完成(2026-06-24):查看侧读口 `GET /api/memories`(含共现 edges)`/api/memory`/`/api/node`(`views.py`)+ 三视图导航骨架 + galaxy 只读显示(`web/view.js`)。

1. 查看侧**编辑写回**(本轮留的):
   - 新建 `editor.py`:`edit_episode`(白名单 overview/summary/source_text/salience_tier/highlights/keywords;**改 overview 须重嵌向量 + 更新 `episode_vectors`**,source_text 走 `episodes_au` 触发器;原子顺序复刻 `archive.confirm_episode`)、`edit_node`(type/aliases,重写 `node_aliases`;改 label 不做)。
   - `POST /api/memory/edit`、`POST /api/node/edit`;galaxy 面板的「编辑」接上(本轮面板有「只读·下一轮」提示位)。
2. 做控制台 API/UI:
   - `GET /api/agent/config`(扩展现有 `/api/agent/providers`)、可选 `POST /api/agent/test`;key 只显掩码,不进前端表单。`#view-console` 现是占位壳。
3. 后续再做单开去噪对比、自动精炼 agent。
4. 工程债可穿插处理:fake provider 写锁、迁移器 applied set、SQLite `busy_timeout`;**zuris 关注的"数据库建边现行做法"待他拍板再动**。

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
python3 -m py_compile memory_system/server.py memory_system/views.py
```

注意:

- 直接用系统 `python3 scripts/verify_s*.py` 可能因包路径或 `sqlite_vec` 未安装失败。优先用 `.venv/bin/python ...`。
- 在受限 sandbox 内直接绑定本地端口可能被拒;HTTP smoke test 如需临时绑定 `127.0.0.1`,允许后再跑。
- `verify_web_api.py` 会临时起 `ThreadingHTTPServer`,覆盖 staging edit/reject/confirm 和删段 HTTP 语义。

## 高优先风险

- fake provider / meta / vec 表锁仍有坑:
  - 真实 `~/.memory_system` 第一次跑 `index rebuild --provider fake` 可能把库锁成 fake meta,之后 DashScope 写入被拒。
  - 建议 README 明确 fake 只用于临时 `MEMORY_SYSTEM_HOME`,或 CLI 加 `--allow-fake-lock`。
- 迁移器仍用 `MAX(version)` 判断当前版本:`memory_system/db/migrate.py`。坏状态 `001,003` 会误报 `002` 已应用。
- SQLite 连接没有 `busy_timeout`:`memory_system/db/connection.py`;GUI/CLI 并发写可能偶发 `database is locked`。
- `preview_cache.sweep_stale` 仍未接线,预览缓存会增长。
- `/api/transcripts` 冷缓存首次列表会 clean 全部 jsonl,大库下可能慢。
- `index rebuild` 对所有 episode overview 全量重嵌;真 DashScope 会联网、耗时、耗额度。

## 文档可信度

可作为当前依据:

- `README.md`:与当前代码接近,但可继续增强 fake provider 风险说明。
- `S5_NOTES.md`:S5 写入侧语义、前端坑位、验收门。
- `project/idea_v2.md`:当前概念正本。
- `project/phase1_build.md`:当前施工脊梁,但部分早期表述与落地实现不同。
- `project/prompts_extraction.md`:Prompt 2 基本可信;Prompt 1 早期文字仍写行号,真实打包 prompt 已改回合制。
- `project/s3_chunking_plan.md`:S3 方案基本可信,但早期 `line_map` / `L-range` 描述已过时。

历史参考,不要按它施工:

- `project/plan_v3.md`
- `project/out/plan_v1.md`
- `project/out/plan_v2.md`
- `project/out/idea.md`

这些旧文档包含 `claude-memory`、`~/.claude-memory`、cron、FastAPI/HTMX、Ollama、activation decay 等旧设定,与当前方向冲突。

## 已修旧项

- `confirm_episode` 的 node/episode 碎片落盘顺序已修,DB/向量失败不污染碎片、不动 staging。
- agent 切块边界已严校,不再静默夹紧坏边界。
- 保存段时禁重叠、允许空洞并返回 gaps。
- S5 删除语义和回归门已补,见 `S5_NOTES.md`。
- 前端 S5 写入侧已拆分静态文件,并完成段预览、轻量撤销修、LLM 在途锁。
- README 已补 `memory-system serve`、`scan`、`index rebuild`。
- `resume.py` 已删除;`/resume` 实测在原 jsonl 追加,不再做跨文件 resume 检测。
- `/api/transcript`、`/api/select` 等 path 已 `_confine()` 到 `cfg.transcripts_root` 内。
- node 文件名已加全 label 短 sha1 后缀,避免清洗/截断/大小写不敏感碰撞。
- frontmatter 写入已拒绝标量/列表项换行,避免 LLM 坏 label 写坏碎片。
- `index rebuild` 已改成先 parse/重嵌,全部成功才 `_clear` DB。
