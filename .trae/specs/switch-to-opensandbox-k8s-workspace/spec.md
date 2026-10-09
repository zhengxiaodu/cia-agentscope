# 切换至 OpenSandbox K8s 工作区集群 Spec

## Why

当前后端使用 `DockerWorkspaceManager`（基于 agentscope `DockerWorkspace`）作为代码执行沙箱，存在单机 Docker 依赖、无法水平扩展、应用重启丢失工作区、容器生命周期与应用耦合、无集中管控与资源配额等瓶颈。引入 OpenSandbox（基于 K8s 的沙箱编排平台）替代单机 Docker，可实现沙箱生命周期由 K8s 集群统一管控、多节点水平扩展、API 化的沙箱创建/销毁/续期，同时保持与现有 agentscope 工具链完全兼容。

采用**配置开关 + 抽象层替换**策略：通过 `WORKSPACE_BACKEND` 环境变量在 `docker` / `opensandbox` 之间切换，两种后端共享相同上层接口，业务代码无需修改，回滚仅需改环境变量并重启。

## What Changes

- **新增** `WORKSPACE_BACKEND` 配置项（`docker` / `opensandbox`，默认 `docker`），控制工作区后端选择。
- **新增** OpenSandbox 连接与沙箱配置项：`OPENSANDBOX_DOMAIN` / `OPENSANDBOX_API_KEY` / `OPENSANDBOX_PROTOCOL` / `OPENSANDBOX_USE_SERVER_PROXY` / `OPENSANDBOX_IMAGE` / `OPENSANDBOX_RESOURCE_CPU` / `OPENSANDBOX_RESOURCE_MEMORY`。
- **新增** 预热池配置项：`OPENSANDBOX_POOL_SIZE` / `OPENSANDBOX_POOL_REFILL`。
- **新增** `OpenSandboxWorkspaceManager`：与 `DockerWorkspaceManager` 接口兼容，底层使用 OpenSandbox SDK；含高可用增强（创建失败指数退避重试、沙箱崩溃自动检测与重建、连续失败降级保护、结构化监控日志）与预热池（消除首次请求冷启动）。
- **新增** `OpenSandboxToolAdapter`：将 OpenSandbox `Sandbox` 封装为与 agentscope `Bash/Read/Write/Edit/Glob/Grep` 等价的操作接口。
- **新增** `opensandbox_tool_bridge.create_opensandbox_tools(adapter)`：将适配层包装为 6 个 `FunctionTool` 实例（名称与 agentscope 内置工具一致），Agent 无感知切换。
- **改造** `main.py` lifespan：根据 `WORKSPACE_BACKEND` 条件初始化 `DockerWorkspaceManager` 或 `OpenSandboxWorkspaceManager`，注入 `OrchestratorService` 并启停 sweeper。
- **改造** `OrchestratorService._build_request_components`：根据后端类型选择工具层（agentscope 原生 / OpenSandbox 桥接）与技能列表获取方式（`workspace.list_skills()` / `manager.list_skills()`）。
- **新增** `deploy/opensandbox/` K8s 部署清单（Server Deployment / Config / RBAC / Namespace / ResourceQuota / API-Key Secret / 镜像同步脚本），作为集群部署参考。
- **新增** 依赖：`opensandbox>=0.1.13`、`opensandbox-code-interpreter>=0.1.0`。

## Impact

- **Affected specs**: `introduce-docker-workspace-manager`（工作区后端由单一 Docker 扩展为双后端，原 spec 的 Docker 路径保留为默认后端）、`multi-turn-session`（工作区获取路径分支化）、`multi-intent-orchestration`（工具层与技能列表获取双后端分支）。
- **Affected code**:
  - 新增 `app/services/opensandbox_workspace_manager.py`
  - 新增 `app/services/opensandbox_adapter.py`
  - 新增 `app/services/opensandbox_tool_bridge.py`
  - `app/config.py`（新增 `WORKSPACE_BACKEND` + OpenSandbox 配置块）
  - `app/main.py`（条件初始化工作区管理器 + 导入新配置）
  - `app/services/orchestrator_service.py`（`_build_request_components` 工具层/技能列表双后端分支 + 导入 `WORKSPACE_BACKEND`）
  - `.env.example`（新增 OpenSandbox 配置段 + `WORKSPACE_BACKEND`）
  - `requirements.txt`（新增 opensandbox 依赖）
  - 新增 `deploy/opensandbox/*`（K8s 部署清单）
- **外部依赖**：需要可访问的 OpenSandbox Server（K8s 集群 + batchsandbox Controller + execd Agent sidecar）；OpenSandbox SDK 0.1.13+；opensandbox-code-interpreter 0.1.0+（Code Interpreter 场景）。
- **已知限制（不在本次范围）**：`/upload` 在 opensandbox 模式下仍写入宿主 `WORKSPACE_BASEDIR/{session_id}`，沙箱为远程 K8s Pod 未 bind-mount 该目录，上传文件在 opensandbox 沙箱内不可直接读取。需后续通过 `sandbox.files.write_files` 推送或 PVC 持久化解决（参考实现未覆盖此路径）。

## ADDED Requirements

### Requirement: 工作区后端配置开关

系统 SHALL 在 `config.py` 提供 `WORKSPACE_BACKEND` 配置项，取值 `docker`（默认）或 `opensandbox`，通过环境变量 `WORKSPACE_BACKEND` 读取。

#### Scenario: 默认使用 Docker 后端
- **WHEN** 未设置 `WORKSPACE_BACKEND` 环境变量
- **THEN** 取值默认为 `docker`，应用使用 `DockerWorkspaceManager`

#### Scenario: 切换到 OpenSandbox 后端
- **WHEN** 设置 `WORKSPACE_BACKEND=opensandbox`
- **THEN** 应用使用 `OpenSandboxWorkspaceManager`，工具层走 OpenSandbox 桥接

### Requirement: OpenSandbox 连接配置

系统 SHALL 提供以下 OpenSandbox 连接与沙箱配置项（均通过环境变量读取，含默认值）：`OPENSANDBOX_DOMAIN`（默认 `localhost:9080`）、`OPENSANDBOX_API_KEY`（默认空）、`OPENSANDBOX_PROTOCOL`（默认 `http`）、`OPENSANDBOX_USE_SERVER_PROXY`（默认 `true`）、`OPENSANDBOX_IMAGE`（默认 `python:3.13-slim`）、`OPENSANDBOX_RESOURCE_CPU`（默认 `100m`）、`OPENSANDBOX_RESOURCE_MEMORY`（默认 `128Mi`）。

### Requirement: 预热池配置

系统 SHALL 提供 `OPENSANDBOX_POOL_SIZE`（默认 `0`，0=不启用）与 `OPENSANDBOX_POOL_REFILL`（默认 `true`）配置项，控制 OpenSandbox 沙箱预热池行为。

### Requirement: OpenSandboxWorkspaceManager 工作区分配

系统 SHALL 提供 `OpenSandboxWorkspaceManager`，与 `DockerWorkspaceManager` 接口兼容（`create_workspace` / `get_workspace` / `close` / `close_all` / `start_sweeper` / `stop_sweeper` / `list_skills` / `list_tools` / `workdir`），底层使用 OpenSandbox SDK。隔离策略：同一 `user_id` 复用同一沙箱，不同 `user_id` 各自独立沙箱；工作路径按 `session_id` 隔离（沙箱内 `/data/workspaces/{session_id}`）。

#### Scenario: 首次请求创建沙箱
- **WHEN** 某 user_id 首次调用 `create_workspace(user_id, session_id, skill_dirs)` 且缓存未命中
- **THEN** 通过 `Sandbox.create` 创建沙箱（带重试），在沙箱内 `mkdir -p {basedir}/{session_id}`，将宿主技能目录内容写入沙箱 `/workspace/skills/`，缓存条目并返回沙箱

#### Scenario: 会话内复用沙箱
- **WHEN** 同一 user_id 后续调用，缓存命中且未超 TTL 且沙箱存活
- **THEN** 切换 workdir 到当前 session 目录，更新最后访问时间，返回已缓存沙箱

#### Scenario: 缓存未命中按需重建
- **WHEN** `get_workspace` 未命中（TTL 过期或沙箱已崩溃）
- **THEN** 淘汰并销毁旧条目，返回 None（由调用方 `create_workspace` 重建）

### Requirement: 工作区缓存与 TTL 回收

系统 SHALL 以 `workspace_id`（= `user-{user_id}`）为键缓存沙箱条目（含 sandbox、last_access、user_id、session_ids、workdir）。空闲超 `ttl` 秒的条目 SHALL 被后台 sweeper 周期淘汰并销毁；访问时懒检查超 TTL 则淘汰。应用关闭时 `close_all()` 销毁全部缓存 + 预热池。

### Requirement: 并发安全

同一 `workspace_id` 的并发创建 SHALL 通过 per-key `asyncio.Lock` 去重，避免为同一用户重复创建多个沙箱。

### Requirement: 高可用 - 创建重试

`OpenSandboxWorkspaceManager` 创建沙箱失败时 SHALL 指数退避重试（最多 3 次，基础延迟 2.0s，`delay = base * 2^(attempt-1)`），全部失败后抛出最后异常。

### Requirement: 高可用 - 崩溃恢复

`get_workspace` 与 `create_workspace` 复用路径 SHALL 通过 `sandbox.commands.run("echo 1")` 探测沙箱存活；不存活（OOMKilled / 节点驱逐）时自动淘汰并重建。

### Requirement: 高可用 - 降级保护

连续失败达阈值（默认 5 次）后 SHALL 进入降级状态：降级期间新请求快速失败（`RuntimeError`），不再尝试创建；降级 60s 后自动尝试恢复探测。进入/退出降级时输出 ERROR 级日志。

### Requirement: 预热池

`OPENSANDBOX_POOL_SIZE > 0` 时，`start_sweeper` SHALL 后台预创建 N 个空闲沙箱；`create_workspace` SHALL 优先从池中分配（池空退化为按需创建），取出后异步补充（`OPENSANDBOX_POOL_REFILL=true` 时）；`close_all` SHALL 销毁池中全部沙箱。

### Requirement: OpenSandboxToolAdapter 工具适配

系统 SHALL 提供 `OpenSandboxToolAdapter`，将 OpenSandbox `Sandbox` 封装为与 agentscope 工具等价的异步接口：`bash(command, timeout)` / `read(path)` / `write(path, content)` / `edit(path, old_text, new_text)` / `glob(pattern, path)` / `grep(pattern, path)` / `ensure_dir(path)` / `list_dir(path)`，并暴露 `workdir` 可读写属性。

### Requirement: opensandbox_tool_bridge FunctionTool 桥接

系统 SHALL 提供 `create_opensandbox_tools(adapter)` 工厂函数，返回 6 个 `FunctionTool` 实例（名称 `Bash` / `Read` / `Write` / `Edit` / `Glob` / `Grep`，与 agentscope 内置工具一致），内部调用 `OpenSandboxToolAdapter` 对应方法，返回 `ToolChunk(content=[TextBlock(text=...)])`。

### Requirement: OpenSandbox 配置项

系统 SHALL 在 `.env.example` 与 `requirements.txt` 补充 OpenSandbox 相关配置项与依赖，保持与 `config.py` 一致。

### Requirement: K8s 部署清单

系统 SHALL 提供 `deploy/opensandbox/` K8s 部署清单（namespace / RBAC / Server Deployment + Config / ResourceQuota / API-Key Secret / 镜像同步脚本），作为 OpenSandbox 集群部署参考。

## MODIFIED Requirements

### Requirement: 应用生命周期管理工作区

`main.py` lifespan SHALL 根据 `WORKSPACE_BACKEND` 条件初始化对应工作区管理器：`opensandbox` 时构造 `ConnectionConfig` + `OpenSandboxWorkspaceManager`（传入连接配置、镜像、basedir=`/data/workspaces`、ttl、resource、ready_timeout、pool_size、pool_refill）；`docker` 时构造 `DockerWorkspaceManager`（保留现有 base_image / basedir / ttl / pip_index_url / pip_trusted_host 参数）。两者均注入 `OrchestratorService.create` 并 `start_sweeper`；关闭阶段 `stop_sweeper` + `close_all`。

#### Scenario: opensandbox 后端启动
- **WHEN** `WORKSPACE_BACKEND=opensandbox` 启动
- **THEN** 构造 `ConnectionConfig(domain, protocol, api_key, use_server_proxy, request_timeout=120s)` 与 `OpenSandboxWorkspaceManager`，打印 `[opensandbox]` 标识日志

#### Scenario: docker 后端启动（默认）
- **WHEN** `WORKSPACE_BACKEND=docker`（或未设置）启动
- **THEN** 构造 `DockerWorkspaceManager`，打印 `[docker]` 标识日志（行为与现状一致）

### Requirement: OrchestratorService 工作区与工具层获取

`OrchestratorService._build_request_components` SHALL 在获取工作区后，根据 `WORKSPACE_BACKEND` 选择工具层：
- `opensandbox`：构造 `OpenSandboxToolAdapter(workspace, workdir=f"/data/workspaces/{session_id_safe}")` + `create_opensandbox_tools(adapter)`，与图表/卡片/mineru 等 FunctionTool 合并为 `all_tools`；技能列表通过 `self._workspace_manager.list_skills(user_id, session_id)` 获取。
- `docker`（默认）：使用 agentscope 原生 `Bash()/Read()/Write()/Edit()/Glob()/Grep()` + 图表/卡片/mineru FunctionTool（保持现状）；技能列表通过 `workspace.list_skills()` 获取。

其余流程（langfuse 工作区 span、`extra_skills` 追加、AgentRegistry 构造、识别器/改写器构建）保持不变。

#### Scenario: opensandbox 后端工具装配
- **WHEN** `WORKSPACE_BACKEND=opensandbox` 且工作区已获取
- **THEN** `all_tools` = `create_opensandbox_tools(adapter)` + 图表/卡片/mineru FunctionTool；`all_skills_meta` 来自 `manager.list_skills()`

#### Scenario: docker 后端工具装配（保持现状）
- **WHEN** `WORKSPACE_BACKEND=docker`
- **THEN** `all_tools` = agentscope 原生工具 + 图表/卡片/mineru FunctionTool；`all_skills_meta` 来自 `workspace.list_skills()`（行为不变）

## REMOVED Requirements

无。Docker 后端代码完整保留，作为默认后端与回滚路径。

## Assumptions & Decisions

1. **配置开关策略**：`WORKSPACE_BACKEND` 控制后端选择，默认 `docker` 零风险，灰度环境切 `opensandbox`，稳定后可全量切换。回滚仅需改环境变量并重启，无需代码变更或数据迁移。
2. **接口兼容**：`OpenSandboxWorkspaceManager` 与 `DockerWorkspaceManager` 暴露相同方法签名（`create_workspace` / `get_workspace` / `close` / `close_all` / `start_sweeper` / `stop_sweeper`），上层 `OrchestratorService` 通过同一接口消费。
3. **技能注入方式差异**：Docker 后端 bind-mount 宿主技能目录；OpenSandbox 后端通过 `sandbox.files.write_files` 将技能文件内容写入沙箱 `/workspace/skills/{skill_name}/`（因远程 Pod 无法 bind-mount）。
4. **工作目录路径**：OpenSandbox 沙箱内工作目录固定为 `/data/workspaces/{session_id}`（manager `basedir=/data/workspaces`）；Docker 后端为宿主 `{WORKSPACE_BASEDIR}/{session_id}` bind-mount 同路径。
5. **高可用常量**：`_MAX_CREATE_RETRIES=3`、`_RETRY_BASE_DELAY=2.0s`、`_DEGRADE_THRESHOLD=5`、`_DEGRADE_RECOVERY_TIME=60s`，为模块级常量。
6. **预热池为可选**：默认 `OPENSANDBOX_POOL_SIZE=0` 不启用，按需创建；生产环境按预期并发新用户数 / 2 配置。
7. **上传文件限制（已知）**：opensandbox 模式下 `/upload` 仍写宿主目录，沙箱内不可直接读取。本次不实现上传文件推送至沙箱（参考实现未覆盖），作为后续工作。
8. **合并策略**：参考代码基于较旧代码库（缺少 langfuse spans / mineru tool / extra_skills / PIP 配置 / chat_tasks），合并时保留当前较新代码库的这些特性，仅注入 OpenSandbox 分支逻辑，不回滚现有功能。
9. **SDK 版本**：`opensandbox>=0.1.13`、`opensandbox-code-interpreter>=0.1.0`（Code Interpreter 场景需要 512Mi+ 内存）。
