# Upload 自动创建 Session / 内置工具替换 / 定时工作区清理 / 文件扫描排除 Spec

## Why

四个独立改进：
1. **Upload 无 sessionId 时自动创建**：当前 `/upload` 不传 `session_id` 则文件落到 `{WORKSPACE_BASEDIR}` 根目录，与后续 chat 的 session 目录脱节，导致容器内无法读取上传文件。应与 `/chat` 一致，自动创建 session 并归档到对应目录。
2. **内置工具替代 workspace.list_tools()**：当前 `all_tools` 来自 DockerWorkspace 容器的 skill 工具列表，但 agent 执行时需要基础文件操作能力（Bash/Read/Write/Edit/Glob/Grep）。改用 agentscope SDK 内置工具类，确保每个 agent 都有这些基础能力。
3. **定时清理过期工作区目录**：`WORKSPACE_BASEDIR` 下会随时间积累大量废弃 session 目录，需后台定时任务按修改时间+保留天数自动清理，释放磁盘。
4. **文件扫描排除 skills/ 和 .mcp**：当前新文件扫描只排除了 `data/`（上传落点），但 agent 编排过程中会在 `skills/` 下写入技能产物、在根级生成 `.mcp` 配置文件。这些不应算作"用户可见的新文件"推给前端。

## What Changes

- **改** `app/routes/upload.py`：`session_id` 为空时通过 `request.app.state.session_service.get_or_create_session(None, user_id)` 获取新 session_id；需要注入 Request 参数。
- **改** `app/services/orchestrator_service.py`：import 内置工具类，将 `all_tools = await workspace.list_tools()` 替换为固定内置工具列表 `[Bash(), Read(), Write(), Edit(), Glob(), Grep()]`。
- **新增** `app/services/workspace_cleanup_service.py`：定时服务，用 `apscheduler` 后台定时器扫描 `{WORKSPACE_BASEDIR}` 下的子目录，删除最后修改时间超过 `WORKSPACE_RETENTION_DAYS` 天的整个目录。
- **改** `app/main.py`：lifespan 启动阶段创建并启动 cleanup scheduler；关闭阶段停止。
- **改** `app/config.py` / `.env` / `.env.example`：新增 `WORKSPACE_RETENTION_DAYS` 配置项（默认 7）。
- **改** `app/services/file_change_detector.py`：`snapshot` 排除规则从仅跳过顶层 `data/` 扩展为同时跳过顶层 `skills/` 以及所有层级下名为 `.mcp` 的文件。
- **改** `requirements.txt`：确认 `apscheduler>=3.10.0` 已存在。

## Impact

- **Affected code**: upload.py, orchestrator_service.py, file_change_detector.py, config.py, .env, .env.example, main.py (lifespan)
- **New files**: workspace_cleanup_service.py
- **External dependencies**: 无新增依赖（`apscheduler` 已有）
- **Breaking**: `all_tools` 从动态（skill 列表）变为固定内置工具列表，agent 不再看到来自 skill 的自定义工具。若后续需要合并两者，可改为 `all_tools = [Bash(), ..., Grep()] + await workspace.list_tools()`。

## ADDED Requirements

### Requirement: Upload 自动获取 Session ID
系统 SHALL 在 `/upload` 接口未传 `session_id` 时，自动通过 `SessionService.get_or_create_session` 创建新会话，并将文件放到对应 session 目录下。

#### Scenario: 未传 session_id
- **WHEN** 用户调用 `/upload` 未传 `session_id` 参数
- **THEN** 通过 `session_service.get_or_create_session(None, user_id)` 获取新 session_id，文件存入 `{WORKSPACE_BASEDIR}/{新session_id}/data/...`

#### Scenario: 已传 session_id
- **WHEN** 用户调用 `/upload` 并传了 `session_id`
- **THEN** 行为不变，直接使用传入的 session_id

### Requirement: 内置工具替换 all_tools
系统 SHALL 用 agentscope SDK 内置工具类替代 `workspace.list_tools()` 作为 `AgentRegistry` 的 `all_tools` 输入。

#### Scenario: 编排执行时
- **WHEN** `_build_request_components` 构建 AgentRegistry
- **THEN** `all_tools = [Bash(), Read(), Write(), Edit(), Glob(), Grep()]`（固定列表），不再调用 `workspace.list_tools()`

### Requirement: 定时工作区清理
系统 SHALL 提供后台定时服务，定期扫描 `WORKSPACE_BASEDIR` 下的子目录，清理最后修改时间超过 `WORKSPACE_RETENTION_DAYS` 天的整个目录。

#### Scenario: 过期目录被清理
- **WHEN** 某个 `{WORKSPACE_BASEDIR}/{session_id}` 目录的最后修改时间距今超过 `WORKSPACE_RETENTION_DAYS` 天
- **THEN** 定时任务递归删除该目录（`shutil.rmtree`）

#### Scenario: 应用关闭时停止清理
- **WHEN** 应用 lifespan 关闭阶段
- **THEN** 清理 scheduler 停止运行

### Requirement: 文件扫描排除 skills/ 和 .mcp
系统 SHALL 在新文件检测扫描中额外排除 `skills/` 子目录和所有层级的 `.mcp` 文件。

#### Scenario: skills/ 下的文件不计入
- **WHEN** agent 向 `{workdir}/skills/...` 写入文件
- **THEN** snapshot 结果不包含该路径

#### Scenario: 根级 .mcp 文件不计入
- **WHEN** agent 生成 `.mcp` 配置文件
- **THEN** snapshot 结果不包含该文件

## MODIFIED Requirements

### Requirement: snapshot 排除规则扩展
`file_change_detector.snapshot` 的排除规则 SHALL 从仅跳过顶层 `data/` 扩展为：跳过顶层 `data/` 和 `skills/` 子目录，以及所有层级下以 `.mcp` 为名的文件。

## Assumptions & Decisions
1. **Upload 获取 session 需要注入 Request**：`get_or_create_session` 需要 `user_id`（已有 `Depends(current_user)` 提供），还需要 `request.app.state.session_service`。因此 upload 函数签名增加 `request: Request` 参数（FastAPI 支持 `request` 作为显式参数注入）。
2. **内置工具不合并 list_tools**：当前需求明确"改用内置工具"，不做合并。如后续需要可追加 `workspace.list_tools()` 到列表尾部。
3. **apscheduler 已存在**：`requirements.txt` 中已有 `apscheduler>=3.10.0`，无需新增依赖。
4. **清理间隔**：默认每 24 小时执行一次（`WORKSPACE_CLEANUP_INTERVAL_HOURS=24`），可通过 `.env` 配置。
5. **清理日志**：每次清理记录删除了哪些目录（logger.info），失败记录 warning 但不中断其他目录清理。
6. **SM3 暂不改**：用户明确表示暂时不改签名算法。
7. **返回前端 session_id**：upload 成功后响应中是否需要返回新创建的 session_id？当前 spec 不改响应结构（DataBlock 已含完整 URL），如需返回可在实施时讨论。
