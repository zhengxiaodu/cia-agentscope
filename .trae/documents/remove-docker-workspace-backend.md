# 删除 Docker 工作区后端，统一为 OpenSandbox 计划

## Summary

项目当前通过 `WORKSPACE_BACKEND` 配置在两种沙箱工作区实现间切换：`docker`（agentscope DockerWorkspace，单机 Docker）与 `opensandbox`（OpenSandbox SDK，K8s 沙箱集群）。本次变更**彻底删除 docker 后端的全部代码逻辑与配置**，OpenSandbox 成为唯一工作区实现：删除后端分支判断、`DockerWorkspaceManager`、宿主机目录清理服务、docker 专属配置项与依赖，相关调用方（main / orchestrator / chat / files）全部改为直连 opensandbox 路径。

## Current State Analysis（探索结论）

- **配置**：[app/config.py](file:///workspace/app/config.py#L89-L103) 定义 `WORKSPACE_BACKEND`（默认 `"docker"`）、docker 专属的 `WORKSPACE_BASE_IMAGE` / `WORKSPACE_BASEDIR` / `WORKSPACE_RETENTION_DAYS` / `WORKSPACE_CLEANUP_INTERVAL_HOURS` / `PIP_INDEX_URL` / `PIP_TRUSTED_HOST`；`WORKSPACE_TTL` 两种后端共用。
- **管理器**：[app/services/workspace_manager.py](file:///workspace/app/services/workspace_manager.py) 整文件即 `DockerWorkspaceManager`；[opensandbox_workspace_manager.py](file:////workspace/app/services/opensandbox_workspace_manager.py) 接口完全兼容（`create_workspace` / `get_workspace` / `close_all` / `start_sweeper` / `stop_sweeper` / `list_session_files` / `read_session_file` / `stat_session_file` / `list_skills`）。
- **启动**：[app/main.py](file:///workspace/app/main.py#L63-L120) lifespan 按 `WORKSPACE_BACKEND` 二选一构造管理器，并额外启动 `WorkspaceCleanupService`（仅清理 docker 宿主机目录 `WORKSPACE_BASEDIR`）；`app.state.workspace_backend` 被注入。
- **编排装配**：[orchestrator_service.py](file:///workspace/app/services/orchestrator_service.py#L495-L553) 按 `WORKSPACE_BACKEND` 分支组装 `all_tools`（docker 分支用 agentscope 原生 `Bash/Read/Write/Edit/Glob/Grep` + `WORKSPACE_BASEDIR` 本地读写闭包装配 md 导出工具）；类型标注引用 `DockerWorkspaceManager`（L104/L125），顶层 import `Bash...`（L29）、`os`（L18）仅 docker 分支使用。
- **文件链路**：[chat_service.py](file:///workspace/app/services/chat_service.py#L270-L313) `_detect_and_emit_files` 与 `generate_response` 快照逻辑均双后端分支（docker 走 `file_change_detector.snapshot/build_file_meta` + `WORKSPACE_BASEDIR`）；[files.py](file:///workspace/app/routes/files.py#L80-L101) 下载路由双后端分支（docker 走宿主机 `FileResponse`）；[chat.py](file:///workspace/app/routes/chat.py#L66) 透传 `workspace_backend`。
- **依赖/部署**：`requirements.txt` 含 `aiodocker`（仅 DockerWorkspace 需要）；`docker-compose.yml` 挂载 `docker.sock` 与 `workspace-data` 卷（仅 docker 工作区需要，app/mysql/redis 服务本身保留）。
- **测试**：[test_orchestrator_component_split.py](file:///workspace/tests/test_orchestrator_component_split.py) 3 处 `monkeypatch.setattr("...orchestrator_service.WORKSPACE_BACKEND", "opensandbox")`（属性删除后会 AttributeError，必须同步删）；[test_sensitive_service.py](file:///workspace/tests/test_sensitive_service.py#L273) 设置 `workspace_backend = "docker"`。
- `.env` 当前已是 `WORKSPACE_BACKEND=opensandbox`；`.env.example` 默认 docker。

## Proposed Changes

### 1. app/config.py — 删除 docker 专属配置
- 删除：`WORKSPACE_BACKEND`、`WORKSPACE_BASE_IMAGE`、`WORKSPACE_BASEDIR`、`WORKSPACE_RETENTION_DAYS`、`WORKSPACE_CLEANUP_INTERVAL_HOURS`、`PIP_INDEX_URL`、`PIP_TRUSTED_HOST`（L89-L103 的 docker 部分）。
- 保留 `WORKSPACE_TTL`，挪入 "OpenSandbox 沙箱配置" 段并注明供 OpenSandboxWorkspaceManager 使用。

### 2. 删除整文件
- `app/services/workspace_manager.py`（`DockerWorkspaceManager`，唯一引用方为 main.py 与 orchestrator 类型标注，本次一并清除）。
- `app/services/workspace_cleanup_service.py`（仅清理 docker 宿主机 `WORKSPACE_BASEDIR` 目录；OpenSandbox 工作区生命周期由其自身 TTL sweeper 管理）。

### 3. app/main.py — 无条件创建 OpenSandbox 管理器
- import 区删除：`WORKSPACE_BACKEND` / `WORKSPACE_BASE_IMAGE` / `WORKSPACE_BASEDIR` / `WORKSPACE_RETENTION_DAYS` / `WORKSPACE_CLEANUP_INTERVAL_HOURS` / `PIP_INDEX_URL` / `PIP_TRUSTED_HOST` / `DockerWorkspaceManager` / `WorkspaceCleanupService`。
- lifespan（L63-L107）：去掉 `if/else`，保留 opensandbox 构造逻辑为唯一路径（含 ConnectionConfig、预热池参数）。
- 删除 `app.state.workspace_backend = WORKSPACE_BACKEND`（L106）。
- 删除 `WorkspaceCleanupService` 启动块（L109-L120）。
- 关闭段 `stop_sweeper/close_all` 保留（opensandbox 管理器同接口）。

### 4. app/services/orchestrator_service.py — 删除 docker 工具装配分支
- L29 删除 `from agentscope.tool import Bash, Read, Write, Edit, Glob, Grep`（仅 docker 分支使用）。
- L18 `import os`、L37-38 `WORKSPACE_BACKEND` / `WORKSPACE_BASEDIR` import 一并删除（`os` 在本文件仅 docker 分支使用）。
- L46 改为 `from app.services.opensandbox_workspace_manager import OpenSandboxWorkspaceManager`；L104 / L125 类型标注 `DockerWorkspaceManager` → `OpenSandboxWorkspaceManager`。
- L495-L553：删除 `if WORKSPACE_BACKEND == "opensandbox":` 判断与整个 docker `else` 分支，opensandbox 路径（adapter、base64 读写闭包、`create_opensandbox_tools + _chart_tools + [policy_qa_tool] + md_tools`、`list_skills`）保留为唯一逻辑并取消一层缩进。

### 5. app/services/chat_service.py — 删除 docker 文件检测分支
- L23 删除 `WORKSPACE_BASEDIR` import；L24 改为 `from app.services.file_change_detector import diff`。
- `generate_response`（L450）删除 `workspace_backend` 参数；L483-486 快照改为仅 opensandbox 路径（`workspace_manager is not None` 时 `list_session_files`，否则空集）。
- `_detect_and_emit_files`（L270-L313）删除 `workspace_backend` 参数与 docker `else` 分支（L302-310），保留 opensandbox 分支并加 `workspace_manager is not None` 守卫；docstring 同步更新。
- L624 删除 `workspace_backend=workspace_backend` 透传。

### 6. app/routes/chat.py — 删除 backend 透传
- L66 删除 `workspace_backend=getattr(request.app.state, "workspace_backend", "docker")` 参数（`workspace_manager` 透传保留）。

### 7. app/routes/files.py — 删除 docker 下载分支
- 删除 `WORKSPACE_BASEDIR` import 与 `workspace_backend` 读取（L70）。
- L80-L101：opensandbox `read_session_file` 成为唯一路径（`workspace_manager is None` 时返回 404 或提示性 500）；删除 docker 宿主机 `realpath` 校验 + `FileResponse` 分支。
- `FileResponse` import 若因此不再使用则删除（`_build_file_response` 用 `Response`，保留）。

### 8. app/services/file_change_detector.py — 精简为仅 diff
- 删除 `snapshot()`（L12）与 `build_file_meta()`（L44）——仅 docker 宿主机路径使用；保留 opensandbox 分支共用的 `diff()`，更新模块 docstring。

### 9. requirements.txt
- 删除 `aiodocker>=0.23.0`（仅 agentscope DockerWorkspace 需要）。

### 10. .env 与 .env.example
- 删除行：`WORKSPACE_BACKEND`、`WORKSPACE_BASE_IMAGE`、`WORKSPACE_BASEDIR`、`WORKSPACE_RETENTION_DAYS`、`WORKSPACE_CLEANUP_INTERVAL_HOURS`、`PIP_INDEX_URL`、`PIP_TRUSTED_HOST` 及 "工作区后端选择" / "Docker 工作区管理器配置" 注释段。
- `WORKSPACE_TTL=3600` 保留（挪入 OpenSandbox 段）；OpenSandbox 段注释去掉 "WORKSPACE_BACKEND=opensandbox 时生效 / 替代 Docker 工作区" 表述。

### 11. docker-compose.yml
- 删除 app 服务的 `/var/run/docker.sock` 挂载与 `workspace-data:${WORKSPACE_BASEDIR...}` 挂载，及 `volumes:` 顶层 `workspace-data` 声明（均为 docker 工作区后端专属；compose 本身仍用于部署 app+mysql+redis）。

### 12. tests 同步
- `tests/test_orchestrator_component_split.py`（L162/L188/L206）：删除 3 处 `monkeypatch.setattr("app.services.orchestrator_service.WORKSPACE_BACKEND", "opensandbox")`（属性已不存在，不删会 AttributeError）。
- `tests/test_sensitive_service.py`（L273）：删除 `request.app.state.workspace_backend = "docker"` 行（`_build_request` mock）。

### 不改动
- `.trae/specs/` 与 `.trae/documents/` 下的历史 spec/计划文档（历史记录，不回改）。
- `md_export_tools.py`、`opensandbox_*` 全家、上传解析链路（与后端选择无关）。

## Assumptions & Decisions

1. `WORKSPACE_BACKEND` 配置项**彻底删除**（而非保留仅允许 opensandbox）——用户明确"把相关代码逻辑和配置都删掉"，opensandbox 为硬编码唯一路径。
2. `WorkspaceCleanupService` 判定为 docker 专属（清理宿主机 `WORKSPACE_BASEDIR`，opensandbox 文件在 K8s 沙箱内，宿主机无该目录）→ 随本次一并删除。若后续需要沙箱内文件清理，属新需求。
3. `PIP_INDEX_URL` / `PIP_TRUSTED_HOST` 仅被 `DockerWorkspaceManager` 用于注入容器 env → 删除（OpenSandbox 镜像源走 `OPENSANDBOX_IMAGE` 体系）。
4. `WORKSPACE_TTL` 两种后端共用 → 保留。
5. docker-compose 仍保留用于部署后端服务本身，仅移除 docker 工作区专属挂载。
6. `.env` 当前 `WORKSPACE_BACKEND=opensandbox`，删除该行后行为不变。

## Verification

1. 全局 grep 确认零残留：`WORKSPACE_BACKEND`、`DockerWorkspaceManager`、`WORKSPACE_BASEDIR`、`WORKSPACE_BASE_IMAGE`、`WorkspaceCleanupService`、`aiodocker`、`workspace_backend`（在 `app/`、`tests/`、`requirements.txt`、`.env*`、`docker-compose.yml` 范围内均无匹配）。
2. `python -c "import app.main"` 与 `python -c "import app.services.orchestrator_service"` 导入无错。
3. 全量回归：`python -m pytest tests/ -q` 全部通过（重点：test_orchestrator_component_split、test_sensitive_service、test_md_export_tools）。
4. 人工核对 orchestrator `_prepare_workspace_components`：opensandbox 装配路径（19 个工具含 4 个 md 导出）逻辑与删除前一致，仅去掉分支外壳。
