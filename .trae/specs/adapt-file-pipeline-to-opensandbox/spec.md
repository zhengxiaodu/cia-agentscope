# 文件链路适配 OpenSandbox 后端 Spec

## Why

OpenSandbox 后端下，智能体生成的文件只存在于沙箱内 `/data/workspaces/{session_id}/`，但现有文件检测（`file_change_detector.snapshot` 走 `os.walk` 宿主机目录）和下载（`files.py` 走 `FileResponse` 宿主机路径）整条链路硬编码宿主机文件系统，导致 `files_generated` 事件永远为空、下载 404，前端无法看到/打开智能体生成的文件。

## What Changes

- 在 `OpenSandboxWorkspaceManager` 上新增会话文件访问能力：`list_session_files` / `read_session_file` / `stat_session_file`，**复用已有的 `OpenSandboxToolAdapter`**（封装了 `sbx.commands.run` / `sbx.files.read_file` / `sbx.files.search`），不在 manager 内直接调 SDK
- `chat_service._detect_and_emit_files` 检测路径按后端分支：OpenSandbox 走管理器新方法，Docker 走原宿主机 `snapshot`
- `files.py` 下载/读取路径按后端分支：OpenSandbox 走管理器 `read_session_file`，Docker 走原 `FileResponse`
- 新增 `GET /files/{session_id}/{path}?mode=inline` 内联读取（返回内容 + 正确 Content-Type），供前端预览面板使用
- 越权校验改为规范化相对路径校验（禁止 `..` / 绝对路径），去掉基于宿主机 `realpath` 的写法
- Docker 后端行为保持原样不动

## Impact

- Affected specs: `detect-and-stream-new-files`（检测链路）、`persist-and-return-session-files`（持久化链路，事件结构不变）、`switch-to-opensandbox-k8s-workspace`（管理器能力扩展）
- Affected code:
  - `app/services/opensandbox_workspace_manager.py`（新增 3 个方法，内部构造 `OpenSandboxToolAdapter` 复用其 bash/read）
  - `app/services/opensandbox_adapter.py`（可能补充 `read_bytes` 用于二进制读取，若现有 `read` 仅返回 str）
  - `app/services/chat_service.py`（`_detect_and_emit_files` 双后端分支 + 透传 `workspace_manager`/`workspace_backend`/`user_id`）
  - `app/routes/files.py`（下载/内联双后端分支 + 越权校验重写）
  - `app/main.py`（确认 `app.state.workspace_manager`/`workspace_backend` 已注入，供 files 路由读取）

## ADDED Requirements

### Requirement: OpenSandbox 会话文件访问能力

`OpenSandboxWorkspaceManager` SHALL 提供 `list_session_files` / `read_session_file` / `stat_session_file` 三个方法，**复用 `OpenSandboxToolAdapter`**（已封装 `sbx.commands.run` / `sbx.files.read_file` / `sbx.files.search`），不在 manager 内直接调 SDK。

#### Scenario: 列出会话文件
- **WHEN** 调用 `list_session_files(user_id, session_id)`
- **THEN** 通过 `OpenSandboxToolAdapter.bash("cd /data/workspaces/{sid} && find . -type f")` 执行，解析 stdout 为相对路径集合（去掉 `./` 前缀，POSIX `/`）；跳过顶层 `data`/`skills` 目录和 `.mcp` 文件；沙箱不存在/目录为空/异常返回空集合，记 warning 不抛

#### Scenario: 读取文本文件
- **WHEN** 调用 `read_session_file(user_id, session_id, rel_path)` 且文件为文本类（md/txt/json/csv/log/代码等）
- **THEN** 通过 `OpenSandboxToolAdapter.read(abs_path)` 读取，返回 bytes（encode）；文件不存在返回 None

#### Scenario: 读取二进制文件
- **WHEN** 调用 `read_session_file(user_id, session_id, rel_path)` 且文件为二进制（图片等）
- **THEN** 通过 `OpenSandboxToolAdapter.bash("base64 -w0 {abs_path}")` 读取 base64，解码为 bytes；文件不存在返回 None

#### Scenario: 查询文件大小
- **WHEN** 调用 `stat_session_file(user_id, session_id, rel_path)`
- **THEN** 通过 `OpenSandboxToolAdapter.bash("stat -c %s {abs_path}")` 解析字节大小（int）；文件不存在返回 None

### Requirement: OpenSandbox 后端文件检测

`chat_service._detect_and_emit_files` SHALL 按 `WORKSPACE_BACKEND` 分支：OpenSandbox 后端用管理器 `list_session_files` 做 before/after 快照、`stat_session_file` 提供大小；`files_generated` 事件结构与持久化逻辑不变。

#### Scenario: OpenSandbox 检测新文件
- **WHEN** OpenSandbox 后端 + 智能体本轮生成新文件
- **THEN** before/after 快照走 `manager.list_session_files`，diff 出新文件后构造 `files_generated` 事件（含 name/path/url/size/media_type），事件结构与 Docker 后端完全一致

#### Scenario: Docker 后端行为不变
- **WHEN** Docker 后端
- **THEN** 检测路径走原 `file_change_detector.snapshot`，行为与改动前完全一致

### Requirement: OpenSandbox 后端文件下载/内联读取

`GET /files/{session_id}/{path}` SHALL 按 `WORKSPACE_BACKEND` 分支：OpenSandbox 后端用管理器 `read_session_file` 读取字节返回；新增 `?mode=inline` 查询参返回内联内容 + 正确 Content-Type 供前端预览。

#### Scenario: OpenSandbox 下载文件
- **WHEN** OpenSandbox 后端 + GET /files/{sid}/{path}（无 mode 或 mode=download）
- **THEN** 走 `manager.read_session_file` 读取字节，返回 `Response(content, media_type=..., headers={"Content-Disposition": "attachment; filename=..."})`

#### Scenario: OpenSandbox 内联预览
- **WHEN** OpenSandbox 后端 + GET /files/{sid}/{path}?mode=inline
- **THEN** 走 `manager.read_session_file` 读取字节，文本类返回 `text/...; charset=utf-8`，图片类返回对应 image mime，`Content-Disposition: inline`

#### Scenario: 越权校验
- **WHEN** path 含 `..` / 绝对路径 / 逃逸 `/data/workspaces/{sid}`
- **THEN** 返回 403，不读取文件

#### Scenario: Docker 后端行为不变
- **WHEN** Docker 后端
- **THEN** 下载路径走原 `FileResponse`，行为与改动前完全一致

## MODIFIED Requirements

### Requirement: 文件检测与下载链路后端感知

原 `detect-and-stream-new-files` 和 `persist-and-return-session-files` 的实现假设文件在宿主机文件系统。现修改为按 `WORKSPACE_BACKEND` 分支：OpenSandbox 后端走管理器沙箱内读取（复用 `OpenSandboxToolAdapter`），Docker 后端走原宿主机路径。`files_generated` 事件结构、`session_files` 表、`append_session_files` 持久化逻辑均不变（前端契约零改动）。
