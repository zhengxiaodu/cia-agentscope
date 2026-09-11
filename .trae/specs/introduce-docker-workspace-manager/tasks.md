# Tasks

- [x] Task 1: 核实 agentscope SDK 的 DockerWorkspace API（结论：`from agentscope.workspace import DockerWorkspace`；构造 keyword-only 含 `base_image`/`host_workdir`(bind-mount 到固定 `/workspace`)/`skill_paths`(宿主目录,SDK 自动 tar seed)/`default_mcps`；方法全 async：`initialize`/`list_tools`/`list_skills`/`close`/`list_mcps`/`reset`；需新增依赖 `aiodocker`）
  - [ ] 在已安装的 agentscope>=2.0 环境中确认 `DockerWorkspace` 的导入路径（预期 `from agentscope.workspace import DockerWorkspace` 或 `agentscope.workspace._docker`）
  - [ ] 确认构造签名：是否接受 `base_image`、宿主目录 bind-mount、`skill_paths`、容器 workdir 等参数
  - [ ] 确认方法：`initialize()` / `list_tools()` / `list_skills()` / `close()`（或等价销毁方法）的名称与是否 async
  - [ ] 若 SDK 类名/签名与预期不符，记录实际 API 并据此调整后续任务的设计

- [x] Task 2: 新增工作区配置项
  - [ ] 在 `app/config.py` 新增 `WORKSPACE_BASE_IMAGE`（默认 `python:3.13-slim`）、`WORKSPACE_BASEDIR`（默认 `/data/docker-workspaces`）、`WORKSPACE_TTL`（默认 `3600`，float 秒）
  - [ ] 在 `.env` 与 `.env.example` 同步新增三项

- [x] Task 3: 实现 DockerWorkspaceManager 类（新文件 `app/services/workspace_manager.py`）
  - [ ] 构造函数 `__init__(self, base_image, basedir, ttl)`，初始化内存缓存 `_cache: dict[workspace_id, Entry]`、per-key 锁 `_locks`、停止事件
  - [ ] `_workspace_id(session_id) -> str` 返回 `session_id`
  - [ ] `create_workspace(user_id, session_id, skill_dirs)`：确保 `{basedir}/{session_id}` 存在；过滤存在的 skill_dirs；构造并 `initialize` DockerWorkspace（bind-mount skill_dirs + session workdir 同路径）；缓存条目（workspace + last_access）；per-key 锁去重；返回 workspace
  - [ ] `get_workspace(user_id, session_id)`：命中且未超 TTL 则刷新 last_access 返回；超 TTL 则淘汰销毁返回 None；未命中返回 None
  - [ ] `close(workspace_id)`：淘汰单条目并销毁底层资源
  - [ ] `close_all()`：销毁并清空全部缓存
  - [ ] `start_sweeper()` / `stop_sweeper()`：后台周期扫描淘汰超 TTL 条目
  - [ ] 单元自测：create→get 命中、TTL 过期淘汰、close、close_all

- [x] Task 4: 在 main.py lifespan 接入 manager
  - [x] 启动阶段：读取 Task 2 配置构造 `DockerWorkspaceManager`，存入 `app.state.workspace_manager`，注入 `OrchestratorService.create(model_config, workspace_manager)`，调用 `manager.start_sweeper()`
  - [x] 关闭阶段：`manager.stop_sweeper()` + `await manager.close_all()`

- [x] Task 5: 改造 OrchestratorService 使用 manager
  - [x] `OrchestratorService.create` 增加 `workspace_manager` 参数并存为 `self._workspace_manager`
  - [x] `_build_request_components` 签名增加 `session_id`
  - [x] `run()` 调用 `_build_request_components` 处透传 `session_id`
  - [x] 在 `merged_skills` 计算完成后（[orchestrator_service.py:244](file:///workspace/app/services/orchestrator_service.py#L244) 之后），用 `self._workspace_manager.get_workspace` → 未命中则 `create_workspace(user_id, session_id, skill_dirs=merged技能目录)`；再 `await workspace.list_tools()` / `await workspace.list_skills()` 喂给 `AgentRegistry`，移除原 `load_skills_from_directories` 调用与 `workdir="./my-workspace"`

- [x] Task 6: 移除 LocalWorkspace 路径
  - [x] 删除 `app/agents/registry.py` 中 `load_skills_from_directories`、`load_all_skills` 及 `from agentscope.workspace import LocalWorkspace`
  - [x] 保留 `AgentRegistry`（仍按 `workspace`/`all_tools`/`all_skills_meta` 泛化消费）
  - [x] 确认无其它调用方引用被删函数

- [x] Task 7: 改造文件上传落点
  - [ ] `app/routes/upload.py`：`FileService(workdir=os.path.join(WORKSPACE_BASEDIR, session_id))`，写入前确保目录存在
  - [ ] `app/services/file_service.py`：保持写入逻辑，workdir 由调用方传入 session 目录
  - [ ] 确认上传文件最终落在 `{WORKSPACE_BASEDIR}/{session_id}/data/...`，与容器 workdir 对齐

- [x] Task 8: 端到端验证（静态核对：1–14 项全部通过；运行时 E2E 待用户在真实环境执行）
  - [x] 首轮 `/chat` 触发 `create_workspace`，容器启动；同 session 第二轮命中复用（不新建容器）（代码级核对通过）
  - [x] 不同 session_id 各自独立容器（代码级核对通过，workspace_id=session_id）
  - [x] `/upload` 后在该 session `/chat`，agent 工具可读取上传文件（代码级核对通过，host_workdir 与 upload 落点对齐）
  - [x] 空闲超 TTL 后容器被淘汰；再次请求重建（代码级核对通过，sweeper + 懒淘汰）
  - [x] 应用关闭时 `close_all()` 清空所有容器（代码级核对通过，lifespan 关闭阶段调用）

# Task Dependencies
- Task 3 依赖 Task 1（需 SDK API 结论）与 Task 2（需配置项）
- Task 4 依赖 Task 2、Task 3
- Task 5 依赖 Task 3、Task 4
- Task 6 依赖 Task 5（确认 chat 路径已切换后再删）
- Task 7 依赖 Task 2
- Task 8 依赖 Task 4、Task 5、Task 6、Task 7
- Task 2 与 Task 1 可并行
