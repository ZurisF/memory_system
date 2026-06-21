# memory_system 交接隐患笔记

> 目的:给后续接手实例快速看懂当前项目真实状态、已知隐患。
> 日期:2026-06-20。

## 已修复(2026-06-20 续场)

- **#1 README 补 serve**:已加 `serve`/`index rebuild`/`scan` + S2 前端范围说明。
- **#3 m003 covered_uuids 冲突注释**:已改。结论锁定:uuid 绝不进碎片;processed 表纯
  操作态书签,**不参与碎片重建**,删库后为空、已处理标记不恢复(可接受代价)。
- **#8 resume 检测**:**整个 `resume.py` 已删**。原实现建立在「resume 复刻进新 jsonl、
  跨文件 uuid 重叠」的**错误前提**上;实测(见 `../project/session-jsonl-lifecycle.md`)
  证明 **resume 永远在原文件追加,uuid 永不跨文件**。`/api/transcript` 的 resume 字段、
  前端断点展示、`verify_s2` 的 resume 测试一并移除。「哪些回合没处理过」由已处理标记覆盖,
  不需要 resume 检测。
- **新增**:transcript 列表自动剔除清洗后 0 回合的空壳(`/clear` stub 等),返回
  `hidden_empty` 计数。

## 安全评估后修复(2026-06-20 续场二)

正本完整性 + 安全四条,趁碎片还没几份焊死:

- **#6 node 文件名碰撞 → 已修**:`_safe_node_filename` 加全 label 短 sha1 后缀,不同
  label(含 macOS 大小写不敏感)永不塌成同名静默覆盖正本。
- **#7 server 任意文件读 → 已修**:`/api/transcript`、`/api/select` 的 path 经 realpath
  校验必须落在 `transcripts_root` 内,越界返回 404。
- **新 A:frontmatter 换行 → 已修**:`_fm_scalar`/`_fm_list` 写入前拒绝含换行的标量/
  列表项,LLM 吐出的坏 label/keyword 在闸口报错,不产出不可解析的碎片。
- **新 B:rebuild 非原子 → 已修**:先 parse 所有碎片 + 联网重嵌(可失败的事全做完),
  全部成功才 `_clear` + 灌库;坏碎片/网络失败不再留下半清空的库。

## 待办(尚未处理)

#4 迁移器 `MAX(version)`、#5 rebuild 全量重嵌(原子性已改善,但仍每次重嵌)、
无 `busy_timeout`(并发写偶发锁)、`sweep_stale` 未接线(preview 缓存无限增长)、
`/api/transcripts` 冷缓存首次列表需 clean 全部文件(可加便宜预筛)。均不阻塞 S3。

## 当前实际状态

- 项目是 Python 包,入口命令是 `memory-system`。
- 已有 S1/S2 的一部分实现:SQLite 迁移、碎片读写、`index rebuild`、transcript 清洗预览、段级 processed 标记、本地前端。
- 本地前端只做到 S2 级别:浏览 transcript(隐藏空壳)、选择回合并标记为已处理。
- 还没有 S3/S4/S5:切块 agent、提取 agent、staging 审核、编辑五件套、归档 active 的完整 GUI 流程。

## 已知隐患

### 1. README 漏写前端启动命令

代码里已经有 `serve` 命令:

- `memory_system/cli.py` 的 `cmd_serve`
- `memory_system/server.py` 的 `serve(cfg, host="127.0.0.1", port=8765)`

但 `README.md` 的 CLI 列表没有写 `memory-system serve`。这会让接手者以为前端不存在或需要另找启动方式。

建议:把 `memory-system serve` 加进 README,注明默认地址 `http://127.0.0.1:8765`。

### 2. 前端范围容易被误会

当前 localhost 前端不是完整审核前端,只是 S2 选段前端。

已有:

- transcript 列表
- transcript 清洗预览
- resume 断点展示
- 回合选择
- 标记选段为已处理

未有:

- 切块 agent
- 人工并段/分段/改边界
- 提取五件套
- staging 审核/编辑
- active/rejected/archived 流转

建议:在 README 或 UI 标题里写明“当前为 S2 选段工具”。

### 3. `covered_uuids` 注释和碎片规则冲突

`memory_system/fragments.py` 明确写了 uuid 绝不进碎片。

但 `memory_system/db/migrations/m003_processed.py` 的注释说 episode 落地后碎片会自带 `covered_uuids`,并可由 `index rebuild` 回填 processed 段。

这两处设计冲突。按当前概念方案与代码实际状态,更合理的是:

- uuid 不进 episode 碎片。
- `processed_segments` 只是 S2 操作态/书签。
- `index rebuild` 不负责恢复 processed 标记。
- processed 表丢了不影响记忆正本。

建议:改掉 `m003_processed.py` 的注释,避免后续实现把 uuid 写回碎片。

### 4. 迁移器用 `MAX(version)` 判断当前版本

`memory_system/db/migrate.py` 用 `MAX(version)` 当当前 schema 版本,`status()` 也按 `m.version <= cur` 判断是否已应用。

隐患:如果数据库出现“001 和 003 已应用,002 缺失”的坏状态,它会误报 002 已应用。

早期个人项目够用,但迁移器更稳的做法是按 `schema_migrations` 的实际 version 集合逐个判断,`up()` 也补齐缺失迁移。

建议优先级:中。等迁移数量继续增加前处理。

### 5. `index rebuild` 会重嵌所有 episode

`memory_system/index.py` 的 rebuild 会清空 DB,从碎片重灌,然后对所有 episode overview 调 embedding provider。

用 fake provider 时没成本;用 DashScope 时会真实联网、耗时、花费额度。

建议:

- README 明确 `index rebuild --provider fake` 适合测试。
- 真库 rebuild 前提示会重嵌。
- 后续可加 `--skip-vectors` 或向量缓存策略。

### 6. node 文件名可能碰撞

`memory_system/fragments.py` 用清洗后的 node label 前 80 字符作为文件名。

两个不同 label 清洗/截断后可能得到同一个文件名,导致覆盖。label 本身才是 node 身份,文件名只是可读句柄。

建议:node 文件名加短 hash 后缀,例如 `清洗后label__a1b2c3.md`。

### 7. server 接受任意 path 参数

`memory_system/server.py` 的 `/api/transcript?path=...` 直接读取传入 path,没有限制必须位于 `cfg.transcripts_root` 下。

虽然服务只绑 `127.0.0.1`,但仍建议做路径边界校验,减少本地误用或页面被诱导请求任意文件的风险。

建议:解析 realpath 后确认 `path` 在 `cfg.transcripts_root` 内。

### 8. resume 检测目前主要靠 uuid 重叠

`memory_system/resume.py` 通过“当前 transcript 开头回合的 uuid 是否都出现在更早 transcript”来识别复刻前缀。

这能工作,但它把 resume 判断绑定在 uuid 行为上。若未来确认存在更可靠的 `last-prompt` / `lastPrompt` / 平台 marker,可以改成 marker 优先、uuid 重叠兜底。

建议优先级:低到中。S2 通过门本来允许人审兜底。

## 建议处理顺序

1. 更新 README:补 `memory-system serve` 和 S2 前端范围。
2. 修正 `m003_processed.py` 关于 `covered_uuids` 的误导注释。
3. 给 server 加 transcript root 路径校验。
4. 给 node 文件名加 hash 防碰撞。
5. 改迁移器状态判断,从 `MAX(version)` 改成按实际 applied set。
6. 给 rebuild 增加真 embedding 成本提示或 `--skip-vectors`。

