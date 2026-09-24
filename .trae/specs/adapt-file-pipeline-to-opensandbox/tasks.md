# Tasks

- [x] Task 1: `OpenSandboxWorkspaceManager` 新增会话文件访问三方法，复用 `OpenSandboxToolAdapter`（改 `app/services/opensandbox_workspace_manager.py`）
  - [x] 新增辅助 `_get_adapter(self, user_id, session_id) -> OpenSandboxToolAdapter`：复用 `create_workspace` 获取沙箱（带重试/降级保护），构造 `OpenSandboxToolAdapter(sandbox, workdir=f"/data/workspaces/{session_id}")`
  - [x] `async def list_session_files(self, user_id: str, session_id: str) -> set[str]`：通过 `adapter.bash("cd /data/workspaces/{sid} && find . -type f")` 执行，解析 stdout 为相对路径集合（去掉 `./` 前缀，POSIX `/`）；跳过顶层 `data`/`skills` 目录和 `.mcp` 文件；沙箱不存在/目录为空/异常返回空集合，记 warning 不抛
  - [x] `async def read_session_file(self, user_id: str, session_id: str, rel_path: str) -> bytes | None`：规范化 rel_path（禁止 `..`/绝对路径）；文本类（md/txt/json/csv/log/代码后缀等）走 `adapter.read(abs_path)` 返回 str 再 encode 为 bytes；二进制（图片等）走 `adapter.bash("base64 -w0 {abs_path}")` 解码 base64 为 bytes；文件不存在返回 None
  - [x] `async def stat_session_file(self, user_id: str, session_id: str, rel_path: str) -> int | None`：通过 `adapter.bash("stat -c %s {abs_path}")` 解析字节大小；文件不存在返回 None

- [x] Task 2: `chat_service._detect_and_emit_files` 双后端分支（改 `app/services/chat_service.py`）
  - [x] 函数签名新增参数 `workspace_manager`、`workspace_backend`、`user_id`（从 generate_response 透传）
  - [x] OpenSandbox 分支：before/after 快照改用 `await workspace_manager.list_session_files(user_id, session_id)`；文件元信息构造改用 `await workspace_manager.stat_session_file(user_id, session_id, rel_path)` 提供 size，`media_type` 仍用 `mimetypes.guess_type(rel_path)`，`url` 仍为 `/files/{session_id}/{rel_path}`
  - [x] Docker 分支：保持原 `snapshot(os.path.join(WORKSPACE_BASEDIR, session_id))` + `build_file_meta(workdir, ...)` 不变
  - [x] generate_response 调用处透传 `workspace_manager=app.state.workspace_manager`、`workspace_backend=app.state.workspace_backend`、`user_id`
  - [x] `files_generated` 事件 payload 与 `append_session_files` 持久化逻辑不变

- [x] Task 3: `files.py` 下载/内联双后端分支 + 越权校验重写（改 `app/routes/files.py`）
  - [x] 从 `request.app.state` 读取 `workspace_manager` 和 `workspace_backend`
  - [x] 越权校验改为规范化相对路径校验：`rel = path.replace("\\", "/").lstrip("/")`；若 `"/.." in rel` 或 `rel.startswith("..")` 或 `os.path.isabs(path)` → 403
  - [x] OpenSandbox 下载分支（无 mode 或 mode=download）：`content = await manager.read_session_file(user_id, session_id, rel)`；None → 404；`media_type = mimetypes.guess_type(rel)[0] or "application/octet-stream"`；返回 `Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{basename}"'})`
  - [x] OpenSandbox inline 分支（mode=inline）：同上读取；文本类（media_type 以 `text/` 开头或 rel 后缀在 md/txt/json/csv/log/代码后缀集合）返回 `text/...; charset=utf-8`；图片类返回对应 image mime；`Content-Disposition: inline`；pdf/docx 等不支持预览的返回 415
  - [x] Docker 分支：保持原 `FileResponse(target, ...)` 不变；inline 模式在 Docker 下也支持（读宿主机文件字节返回，逻辑与 OpenSandbox inline 一致）
  - [x] 需要 `user_id` 用于 OpenSandbox 管理器定位沙箱（从 `current_user` 依赖获取）

- [x] Task 4: 语法校验与验证
  - [x] `python -m py_compile` 通过修改的文件
  - [x] grep 确认 OpenSandbox 分支仅在 `workspace_backend == "opensandbox"` 时走管理器，Docker 分支保持原逻辑
  - [x] 核对 Docker 后端默认行为不变（WORKSPACE_BACKEND 未设置时走原宿主机路径）
  - [x] 核对 `files_generated` 事件结构与 `session_files` 持久化未改变

# Task Dependencies
- Task 2 依赖 Task 1（需管理器 list_session_files/stat_session_file）
- Task 3 依赖 Task 1（需管理器 read_session_file）
- Task 4 依赖 Task 1-3 全部完成
