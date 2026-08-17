# 上传文件写入沙箱工作路径实施计划

## Summary

当前 `POST /upload` 接口把文件写到宿主机 `WORKSPACE_BASEDIR/{session_id}/data/`（仅对 Docker 后端有效）。OpenSandbox 后端下，文件不会进沙箱，智能体读不到。本计划在 `OpenSandboxToolAdapter` 新增 `upload` 方法（底层仍用 `self._sandbox.files.write_files`，但与 `write` 区分），并改造 `routes/upload.py` 让 OpenSandbox 后端把文件写入沙箱的 `adapter._workdir`（即 `/data/workspaces/{session_id}/`）。Docker 后端保持原宿主机落盘逻辑不变。

## Current State Analysis

- **`app/services/opensandbox_adapter.py`**：`OpenSandboxToolAdapter` 持有 `self._sandbox` 与 `self._workdir`。已有 `write(path, content: str)` 方法，底层 `WriteEntry(path=path, data=content, mode=644)`，`data` 接收 `str`。
- **`app/routes/upload.py`**：读取 `file` → `FileService.validate_file_size` / `validate_media_type` → 用 `WORKSPACE_BASEDIR` 拼宿主机 workdir → `FileService.save_upload` 写盘 → 返回 `UploadResponse` 含 `DataBlock`。`user_id` 来自 `current_user`，`session_id` 可由 `session_service.get_or_create_session` 自动创建。未读取 `app.state.workspace_backend` / `app.state.workspace_manager`。
- **`app/services/file_service.py`**：`save_upload` 把 bytes 写到 `{workdir}/data/{uuid}_{filename}`，返回 `DataBlock(id, name, URLSource(url=file://绝对路径, media_type))`。仅对宿主机有效。
- **`app/services/opensandbox_workspace_manager.py`**：已暴露 `_get_adapter(user_id, session_id)` 返回 `OpenSandboxToolAdapter(sbx, workdir=self._session_dir(session_id))`（私有方法，但在同包内可调用）；`_session_dir(session_id) -> "/data/workspaces/{session_id}"`。
- **`app/main.py`**：已设置 `app.state.workspace_backend = WORKSPACE_BACKEND`、`app.state.workspace_manager = workspace_manager`。
- SDK 未在沙箱中安装，但 `WriteEntry` 现有用法是 `data=str`；二进制（图片/pdf）需用 base64 文本写入或确认 SDK 是否接受 bytes。鉴于现有 `read_session_file` 已用 `adapter.bash("base64 -w0 ...")` 读二进制，本计划采用「写入侧也走 base64 文本 + 在沙箱内 base64 解码」的稳健做法，避免 SDK `data` 字段类型约束。

## Proposed Changes

### 1. `app/services/opensandbox_adapter.py` — 新增 `upload` 方法

在 `write` 方法之后新增 `upload` 方法，与 `write` 区分：

- **签名**：`async def upload(self, path: str, content: bytes) -> str`
- **行为**：
  - 自动确保父目录存在：`await self.ensure_dir(os.path.dirname(path))`（若 `dirname` 为空跳过）。
  - 将 `content` base64 编码为文本，通过 `self._sandbox.files.write_files([WriteEntry(path=tmp_b64_path, data=b64_str, mode=644)])` 写入一个临时 `.b64` 文件，再用 `self._sandbox.commands.run(f"base64 -d {tmp_b64_path} > {path} && rm -f {tmp_b64_path}")` 解码到目标路径。这样 `data` 始终是 `str`，规避 SDK 二进制约束。
  - 临时文件路径：`path + ".upload.b64"`，写完即删。
  - 返回写入后的目标路径 `path`（绝对路径，落在 `self._workdir` 下由调用方拼好）。
- **与 `write` 区分**：`write(path, content: str)` 处理文本（智能体代码生成场景），`upload(path, content: bytes)` 处理用户上传的任意二进制文件；底层都用 `write_files`，但 `upload` 多了 base64 编解码步骤和目录确保。
- **导入**：顶部新增 `import base64` 和 `import os`。

### 2. `app/routes/upload.py` — 双后端分支

在现有 `/upload` 路由内，校验通过后按 `workspace_backend` 分支：

- 从 `request.app.state` 读取 `workspace_backend` 和 `workspace_manager`。
- **OpenSandbox 分支**（`workspace_backend == "opensandbox" and workspace_manager is not None`）：
  - 调用 `workspace_manager._get_adapter(user_id, session_id)` 获取 adapter（`adapter._workdir` 即 `/data/workspaces/{session_id}`）。
  - 目标路径：`adapter.workdir + "/" + unique_name`，其中 `unique_name = f"{uuid.uuid4().hex}_{filename}"`（沿用 `FileService` 命名规则）。
  - 调用 `await adapter.upload(target_path, content)` 写入沙箱。
  - 构造 `DataBlock`：`id=uuid.uuid4().hex`、`name=filename`、`URLSource(url=AnyUrl(f"sandbox://{session_id}/{unique_name}"), media_type=media_type)`。url 用 `sandbox://` 伪协议标识沙箱内文件（避免 `file://` 指向不存在的宿主机路径），与 `FileService` 返回结构一致，前端契约不变。
- **Docker 分支**（默认）：保持原 `FileService(workdir=...).save_upload(...)` 逻辑完全不变。
- `UploadResponse` 结构不变。

### 3. 不改动文件（明确范围）

- `app/services/file_service.py`：Docker 后端沿用，不改。
- `app/services/opensandbox_workspace_manager.py`：已有 `_get_adapter` / `workdir` 可复用，不改。
- `app/models/upload.py`：响应模型不变。

## Assumptions & Decisions

1. **二进制写入采用 base64 中转**：SDK 未在沙箱内安装，无法确认 `WriteEntry.data` 是否接受 `bytes`；现有 `read_session_file` 已采用 base64 读取二进制，写入侧对称使用 base64 最稳妥，保证 `data` 始终为 `str`。
2. **复用 `_get_adapter`**：虽然带下划线，但同项目内复用已有方法，避免在 manager 上再开公开方法（与 `list_session_files` 等内部用 `_get_adapter` 一致）。
3. **`DataBlock.url` 用 `sandbox://` 伪协议**：避免 `file://` 指向宿主机不存在的路径误导前端；仅作标识，前端读取沙箱文件走 `/files/{session_id}/{rel_path}` 接口（已在本分支修复）。
4. **目录自动创建**：`upload` 内 `ensure_dir` 保证 `adapter.workdir` 下任意子路径可写。
5. **不修改 `write` 方法**：保持智能体代码生成场景的文本写入路径稳定。
6. **Docker 后端行为零改动**：`WORKSPACE_BACKEND` 未设置默认 `docker`，走原 `FileService` 宿主机落盘。

## Verification

1. `python -m py_compile app/services/opensandbox_adapter.py app/routes/upload.py` 退出码 0。
2. Grep 确认 `upload` 方法与 `write` 方法并存且签名不同（`bytes` vs `str`）。
3. Grep 确认 `routes/upload.py` 同时存在 `opensandbox` 分支（调 `adapter.upload`）和 Docker 分支（调 `FileService.save_upload`）。
4. 核对 Docker 后端默认行为不变（`WORKSPACE_BACKEND` 未设置走原路径）。
5. 提交并推送到远程分支 `trae/agent-5CYjia`。
