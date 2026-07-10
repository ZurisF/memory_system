# server.py 定点重构报告

完成日期: 2026-07-10

## 完成内容

1. `/api/transcript` 不再向浏览器返回每回合 `uuids`。响应保留
   `idx / human_text / assistant_text / msg_count / processed`，选段仍只提交
   `turn_idxs`，uuid 映射和 processed 计算留在服务端。
2. 新增 `memory_system/agent/provider_admin.py`，从 `server.py` 移出 provider
   配置视图、角色设置更新、自定义 provider id/base_url 规则、配置落盘、删除后的
   override 清理，以及进程内 agent 热状态。
3. `Config`/`AgentConfig` 继续作为 frozen 启动快照。Web 服务的当前 agent 设置由
   `provider_admin` 持有；provider 列表、探活、切段、提取和 recall 重构均读取当前状态。
   `memory_system/` 内已无 `object.__setattr__`。

## 修改文件

- `memory_system/agent/provider_admin.py`: 新增 provider 管理和运行时设置模块。
- `memory_system/server.py`: provider handler 改为薄 HTTP 适配层；接入热状态；移除
  transcript `uuids`。
- `memory_system/agent/registry.py`: 更新锁职责注释，provider 目录知识仍只保留在此模块。
- `scripts/verify_provider_config.py`: 增加新模块直测、落盘顺序、override 清理及 frozen
  Config 不变性回归。
- `scripts/verify_web_api.py`: 增加 transcript 不含 `uuids`、`turn_idxs` 选段仍可用的门。
- `ARCHITECTURE.md`: 更新 provider 管理、运行时配置和 server 职责说明。
- `REFACTOR_REPORT.md`: 本报告。

未修改 `memory_system/recall/`、碎片/DB 逻辑、CLI、前端 JS、`review_ideas.md` 或
`suggest.md`。前端 JS 经检索没有读取 transcript `uuids`，因此无需改动。

## 新模块接口

- `initialize_runtime(cfg)`: 用 frozen 启动快照初始化该服务实例的 agent 热状态。
- `current_agent(cfg)`: 读取当前 `AgentConfig`。
- `runtime_config(cfg)`: 返回替换了当前 agent 设置的新 `Config` 视图，不修改原对象。
- `get_agent_settings(cfg)`: 生成控制台配置视图，并刷新 dotenv key 状态和自定义目录。
- `get_provider_listing(cfg)`: 生成 provider 列表及 chunk/extract 当前默认值。
- `update_role_settings(cfg, role, *, provider, model)`: 持久化并热更新角色设置。
- `add_custom_provider(cfg, name, base_url, model="")`: 校验、生成 id、写占位 key 并新增。
- `update_custom_provider(cfg, provider_id, *, name, base_url, model)`: 更新可变元数据。
- `remove_custom_provider(cfg, provider_id)`: 删除目录项并清理全局/三角色悬空 override。

业务模块使用语义化异常；HTTP 400/403/404/409/500 的映射只存在于 `server.py`。

## 写入语义

- 新增 provider: 先用 `update_dotenv` 原子更新 `.env`，再用 `save_custom` 原子替换
  `custom_providers.json`，与旧 handler 顺序一致。
- 修改 provider: 原子替换 `custom_providers.json` 后更新进程内状态。
- 删除 provider: 先原子替换 `custom_providers.json`，再写 `.env` 清理悬空 override；
  provider key 变量仍保留，行为不变。
- 角色更新: `.env` 原子写成功后才替换进程内 `AgentConfig`。

## 验收结果

运行前已按施工书设置 `no_proxy=127.0.0.1,localhost` 并清除所有代理变量。

### `.venv/bin/python scripts/verify_provider_config.py`

退出码 0。输出要点:

```text
[ok] provider_admin:新增先写 .env 再写目录;删除先写目录再清 override;Config 快照不变
[ok] HTTP:自定义 provider 可修改
[ok] HTTP:删除自定义 provider 后清理 role override
[ok] HTTP:POST recall provider=fake 写 .env 并回读;全局 provider 不受影响
Provider config regressions ALL PASS
```

### `.venv/bin/python scripts/verify_web_api.py`

退出码 0。输出要点:

```text
[ok] GET /api/transcript:回合载荷不含 uuids,其余字段契约不变
[ok] POST /api/select:只传 turn_idxs 仍可在服务端映射 uuid 并回显 processed
Web API staging contract ALL PASS
POST /api/recall 门 ALL PASS
```

### `.venv/bin/python scripts/verify_view_api.py`

退出码 0。输出终行:

```text
View API read contract ALL PASS
```

附加检查:

```text
.venv/bin/python -m compileall -q memory_system scripts  # exit 0
git diff --check                                         # exit 0
grep -RIn "object\.__setattr__" memory_system --include='*.py'  # 零输出
```

## 偏离与说明

- 无实现范围偏离。为保持既有响应 JSON，角色更新响应中的 `restart_required` 和 `hint`
  字段原样保留；当前进程的切段、提取、探活和重构实际已读取显式热状态。
- 当前环境没有 `rg` 命令，零命中验收改用上述等价 `grep`；检查范围仍为
  `memory_system/` 的 Python 生产代码。
- 未创建 git commit。
