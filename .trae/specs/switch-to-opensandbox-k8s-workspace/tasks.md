# Tasks

- [x] Task 1: 新增 OpenSandbox 核心服务文件（3 个新文件，自包含，无依赖）
  - [x] SubTask 1.1: 新增 `app/services/opensandbox_workspace_manager.py`（含高可用重试/崩溃恢复/降级/预热池）
  - [x] SubTask 1.2: 新增 `app/services/opensandbox_adapter.py`（Sandbox → agentscope 工具操作适配）
  - [x] SubTask 1.3: 新增 `app/services/opensandbox_tool_bridge.py`（adapter → 6 个 FunctionTool 桥接）
- [x] Task 2: 配置层改造（`config.py` + `.env.example` + `requirements.txt`）
  - [x] SubTask 2.1: `app/config.py` 新增 `WORKSPACE_BACKEND` + OpenSandbox 连接/沙箱/预热池配置项
  - [x] SubTask 2.2: `.env.example` 新增 OpenSandbox 配置段 + `WORKSPACE_BACKEND`（保留现有 PIP 配置）
  - [x] SubTask 2.3: `requirements.txt` 新增 `opensandbox>=0.1.13` + `opensandbox-code-interpreter>=0.1.0`
- [x] Task 3: `app/main.py` lifespan 条件初始化工作区管理器
  - [x] SubTask 3.1: 导入新增的 OpenSandbox 配置项
  - [x] SubTask 3.2: 根据 `WORKSPACE_BACKEND` 分支构造 `OpenSandboxWorkspaceManager`（含 `ConnectionConfig`）或 `DockerWorkspaceManager`（保留 pip 参数），打印对应标识日志
  - [x] SubTask 3.3: 保留现有 `app.state.workspace_manager` 赋值、`start_sweeper`、关闭阶段 `stop_sweeper`+`close_all`、chat_tasks 注册表等不变
- [x] Task 4: `app/services/orchestrator_service.py` 工具层/技能列表双后端分支
  - [x] SubTask 4.1: 导入 `WORKSPACE_BACKEND`
  - [x] SubTask 4.2: `_build_request_components` 中根据 `WORKSPACE_BACKEND` 选择工具层（opensandbox 桥接 / agentscope 原生），保留 mineru_parse_tool 与图表/卡片 FunctionTool
  - [x] SubTask 4.3: 技能列表获取双后端分支（`manager.list_skills()` / `workspace.list_skills()`），保留 langfuse 工作区 span 与 extra_skills 逻辑不变
- [x] Task 5: 新增 K8s 部署清单 `deploy/opensandbox/`
  - [x] SubTask 5.1: namespace.yaml / rbac.yaml / resource-quota.yaml / api-key-secret.yaml
  - [x] SubTask 5.2: server-deployment.yaml / server-config.yaml / sync_opensandbox_images.sh / README.md
- [x] Task 6: 语法校验与验证
  - [x] SubTask 6.1: `python -c "import ast; ast.parse(...)"` 校验所有改动文件
  - [x] SubTask 6.2: 校验 docker 后端默认路径行为不变（WORKSPACE_BACKEND 未设置时走原逻辑）

# Task Dependencies
- Task 2、Task 5 与 Task 1 无依赖，可并行
- Task 3 依赖 Task 1（导入新管理器）与 Task 2（导入新配置）
- Task 4 依赖 Task 1（导入 adapter/bridge）与 Task 2（导入 WORKSPACE_BACKEND）
- Task 6 依赖 Task 1-5 全部完成
