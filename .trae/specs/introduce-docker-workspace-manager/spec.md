# 引入 DockerWorkspaceManager 工作区管理器 Spec

## Why

当前后端在每次 `/chat` 请求时新建一个 `LocalWorkspace(workdir="./my-workspace")`（[orchestrator_service.py:250](file:///workspace/app/services/orchestrator_service.py#L250)），所有用户、所有会话共享同一宿主磁盘目录，无法按租户/会话隔离，也无法管理底层资源（容器、MCP 进程）的生命周期。官方 `WorkspaceManager` 绑定 agentservice 运行时，无法在自研 FastAPI 中复用。需要自建一个 `DockerWorkspaceManager`，按 `session_id` 隔离分配 Docker 工作区，缓存并按 TTL 回收，让多会话互不干扰且资源可控。

## What Changes

- **新增** `DockerWorkspaceManager` 类：以 `workspace_id`（= `session_id`）为键在内存缓存 workspace，支持 `create_workspace` / `get_workspace` / `close` / `close_all`，空闲超 TTL 淘汰并销毁底层容器资源，按 `session_id` 隔离（同一 session_id 复用同一 workspace）。
- **新增** 系统初始化阶段在 `main.py` lifespan 构造 `DockerWorkspaceManager(base_image, basedir, ttl)`，启动后台 TTL 清扫任务；应用关闭时 `close_all()`。
- **替换** `OrchestratorService._build_request_components` 中的 `load_skills_from_directories`（LocalWorkspace）调用，改为向 manager 申请/复用 Docker workspace；首轮创建时定型技能集，会话内后续轮次复用。
- **移除** `app/agents/registry.py` 中 `load_skills_from_directories` / `load_all_skills` 及 `LocalWorkspace` 导入（不再走 chat 路径）。
- **改造** `/upload` 与 `FileService`：上传文件落入对应 session 的工作区目录 `{WORKSPACE_BASEDIR}/{session_id}`，与容器 workdir 保持一致以便容器内可读。
- **新增配置项**：`WORKSPACE_BASE_IMAGE`、`WORKSPACE_BASEDIR`、`WORKSPACE_TTL`（写入 `.env` 与 `config.py`）。

## Impact

- **Affected specs**: `multi-turn-session`（会话隔离维度变化）、`file-upload-attachment`（上传落点变化）、`multi-intent-orchestration`（动态技能注入路径变化）。
- **Affected code**:
  - 新增 `app/services/workspace_manager.py`
  - `app/main.py`（lifespan 初始化/关闭 manager + 启停清扫任务）
  - `app/services/orchestrator_service.py`（`create` 接收 manager；`_build_request_components` 增加 `session_id` 并改用 manager；`run` 透传 `session_id`）
  - `app/agents/registry.py`（移除 LocalWorkspace 路径，保留 `AgentRegistry` 泛化消费 workspace）
  - `app/routes/upload.py`、`app/services/file_service.py`（workdir 改为 `{WORKSPACE_BASEDIR}/{session_id}`）
  - `app/config.py`、`.env`、`.env.example`（新增三项配置）
- **外部依赖**：需要宿主可访问 Docker daemon；agentscope SDK 需暴露 `DockerWorkspace` 类（导入路径与构造/方法签名需在实现前核实）。

## ADDED Requirements

### Requirement: DockerWorkspaceManager 工作区分配

系统 SHALL 提供一个 `DockerWorkspaceManager`，在应用启动时以 `base_image`、`basedir`、`ttl` 构造，负责按会话分配、缓存与回收 Docker 工作区。`workspace_id` SHALL 等于 `session_id`（隔离策略 = 按 `session_id` 隔离，同一 session_id 分配同一 workspace）。

#### Scenario: 首次请求创建工作区
- **WHEN** orchestrator 在某 session_id 首次调用 `get_workspace(user_id, session_id)` 返回空
- **THEN** 调用 `create_workspace(user_id, session_id, skill_dirs)` 构造并初始化一个 Docker workspace，绑定传入的 `skill_dirs`（bind-mount 宿主技能目录），将 `workspace_id=session_id` 写入内存缓存，记录最后访问时间，返回该 workspace

#### Scenario: 会话内复用工作区
- **WHEN** 同一 session_id 后续轮次再次申请工作区
- **THEN** `get_workspace` 命中缓存，直接返回已存在的工作区；**不**重新应用当轮的 `skill_dirs`（首轮定型，会话内技能集不再变）

#### Scenario: 缓存未命中按需重建
- **WHEN** `get_workspace` 未命中（已被 TTL 淘汰或显式 close）
- **THEN** 由调用方再次 `create_workspace` 重建（用当轮 `skill_dirs`）

### Requirement: 工作区缓存与 TTL 回收

系统 SHALL 以 `workspace_id` 为键在内存中缓存 workspace 条目（含 workspace 对象与最后访问时间戳）。空闲超过 `ttl` 秒的条目 SHALL 被淘汰并销毁底层资源（容器、MCP 进程）。

#### Scenario: 后台清扫淘汰空闲工作区
- **WHEN** 后台 TTL 清扫任务（lifespan 启动，周期性扫描）发现某条目 `last_access + ttl < now`
- **THEN** 淘汰该条目并销毁其底层容器/MCP 资源，从缓存移除

#### Scenario: 访问时懒淘汰
- **WHEN** `get_workspace` 命中但条目已超 TTL
- **THEN** 视为未命中，先淘汰销毁该条目，返回空（由调用方重建）

#### Scenario: 应用关闭清空缓存
- **WHEN** lifespan 进入关闭阶段
- **THEN** 取消后台清扫任务，调用 `close_all()` 销毁并清空所有缓存条目

### Requirement: 单条目回收

系统 SHALL 提供 `close(workspace_id)` 淘汰单个缓存条目并销毁其底层资源；SHALL 提供 `close_all()` 清空全部缓存。

### Requirement: 并发安全

同一 `workspace_id` 的并发创建 SHALL 通过 per-key 锁去重，避免为同一 session 重复启动多个容器。

#### Scenario: 同 session 并发首请求
- **WHEN** 同一 session_id 的多个请求几乎同时到达且缓存为空
- **THEN** 仅第一个请求执行 `create_workspace` 真正创建容器，其余请求等待并复用同一结果

### Requirement: 容器工作区目录与上传文件对齐

每个 session 的 Docker workspace 的容器 workdir SHALL 与宿主 `{WORKSPACE_BASEDIR}/{session_id}` 通过 bind-mount 对齐（同路径），使 `/upload` 写入宿主该目录的文件在容器内可读。

#### Scenario: 上传文件可被容器内 agent 读取
- **WHEN** 用户先调用 `/upload`（session_id=S）上传文件，再在该 session 内 `/chat`
- **THEN** 文件落在宿主 `{WORKSPACE_BASEDIR}/{session_id}/data/...`，容器 workdir bind-mount 同路径，agent 工具可读取该文件

### Requirement: 工作区配置项

系统 SHALL 在 `.env` / `config.py` 新增三项可配：`WORKSPACE_BASE_IMAGE`（默认 `python:3.13-slim`）、`WORKSPACE_BASEDIR`（默认 `/data/docker-workspaces`）、`WORKSPACE_TTL`（默认 `3600`，单位秒）。

#### Scenario: 启动读取配置构造 manager
- **WHEN** 应用启动
- **THEN** lifespan 读取上述三项配置构造 `DockerWorkspaceManager(base_image=..., basedir=..., ttl=...)`

### Requirement: 技能目录 bind-mount

`create_workspace` SHALL 仅 bind-mount 宿主上真实存在的技能目录；缺失的技能目录 SHALL 跳过并记录警告，不阻断工作区创建。

## MODIFIED Requirements

### Requirement: OrchestratorService 工作区获取

`OrchestratorService._build_request_components` SHALL 接收 `session_id` 参数（由 `run` 透传），并在计算出 `merged_skills` 后，通过 `DockerWorkspaceManager` 获取/创建 workspace（`get_workspace` 未命中则 `create_workspace`），再调用 `workspace.list_tools()` / `workspace.list_skills()` 填充 `AgentRegistry`，替代原 `load_skills_from_directories`。`OrchestratorService.create` SHALL 接收并持有 `workspace_manager`。

#### Scenario: 首轮创建并定型技能
- **WHEN** 某 session 首轮对话，`merged_skills` 计算完成
- **THEN** `get_workspace` 未命中 → `create_workspace(user_id, session_id, skill_dirs=merged技能目录)` 创建并初始化 Docker workspace，其工具/技能元数据喂给 `AgentRegistry`

#### Scenario: 后续轮次复用
- **WHEN** 同一 session 后续轮次
- **THEN** `get_workspace` 命中，返回首轮 workspace；当轮 `merged_skills` 不再重新应用

### Requirement: 文件上传落点

`/upload` 路由与 `FileService` SHALL 将文件写入 `{WORKSPACE_BASEDIR}/{session_id}`（而非共享 `./my-workspace`），并在写入前确保该目录存在。

### Requirement: 应用生命周期管理工作区

`main.py` lifespan SHALL 在启动阶段构造 `DockerWorkspaceManager` 并存入 `app.state.workspace_manager`、注入 `OrchestratorService.create`、启动后台 TTL 清扫任务；在关闭阶段取消清扫任务并 `close_all()`。

## REMOVED Requirements

### Requirement: 每请求新建 LocalWorkspace
**Reason**: 改为按 session 复用的 Docker workspace，原 LocalWorkspace 每请求新建、共享 `./my-workspace` 的模式被取代。
**Migration**: `app/agents/registry.py` 中的 `load_skills_from_directories` / `load_all_skills` 及 `from agentscope.workspace import LocalWorkspace` 移除；`orchestrator_service.py` 中 `workdir="./my-workspace"` 硬编码移除；`upload.py` / `file_service.py` 中 `workdir="./my-workspace"` 改为 `{WORKSPACE_BASEDIR}/{session_id}`。

## Assumptions & Decisions

1. **自建 manager + 复用 SDK `DockerWorkspace`**：manager 的分配/缓存/TTL/隔离/回收逻辑自研；每个 workspace 复用 agentscope SDK 的 `DockerWorkspace`（负责实际容器/MCP/skill）。SDK 的 `DockerWorkspace` 导入路径与构造/方法签名（是否支持 `base_image`、bind-mount、`skill_paths`、`initialize`/`list_tools`/`list_skills`/`close`）需在实现前核实，作为前置任务。
2. **`workspace_id = session_id`**：隔离策略为按 `session_id` 隔离，缓存键即 session_id。
3. **首轮定型技能**：会话内首轮创建后技能集不再变；mng 权限更新需新会话才生效。
4. **bind-mount 宿主技能目录**：基础技能 `/workspace/skills/*`、外部技能 `{EXTERNAL_SKILLS_DIR}/{code}` 的宿主目录 bind-mount 进容器；缺失目录跳过+警告。
5. **容器 workdir 与宿主 session 目录同路径对齐**：宿主 `{WORKSPACE_BASEDIR}/{session_id}` bind-mount 到容器同路径并作为 workdir，保证上传文件容器内可读。
6. **base_image = `python:3.13-slim`**（可配）。
7. **TTL 双机制**：后台周期清扫（主动）+ 访问时懒检查（被动）。
8. **Agent 构造不变**：`Agent(...)` 不接收 workspace 参数（现状如此），workspace 仅在 registry 层消费（`list_tools`/`list_skills`）；Docker workspace 返回的工具天然通过 gateway 在容器内执行，无需改 agent 代码。
