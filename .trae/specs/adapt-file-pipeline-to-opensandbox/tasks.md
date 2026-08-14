# Tasks

- [ ] Task 1: `OpenSandboxWorkspaceManager` 新增会话文件访问三方法（改 `app/services/opensandbox_workspace_manager.py`）
  - [ ] `async def list_session_files(self, user_id: str, session_id: str) -> set[str]`：复用 `create_workspace` 获取沙箱（带重试/降级保护）；执行 `cd /data/workspaces/{session_id} && find . -type f`，解析 stdout 为相对路径集合（去掉 `./` 前缀，POSIX `/`）；跳过顶层 `data`/`skills` 目录和 `.mcp` 文件；沙箱不存在/目录为空/异常返回空集合，记 warning 不抛
  - [ ] `async def read_session_file(self, user_id: str, session_id: str, rel_path: str) -> bytes | None`：规范化 rel_path（禁止 `..`/绝对路径）；文本类（md/txt/json/csv/log 等）走 `sbx.files.read_file(abs_path)` 返回 bytes；二进制（图片等）走 `sbx.commands.run(f"base64 -w0 {abs_path}")` 解码 base64 为 bytes；文件不存在返回 None
  - [ ] `async def stat_session_file(self, user_id: str, session_id: str, rel_path: str) -> int | None`：执行 `stat -c %s {abs_path}` 解析字节大小；文件不存在返回 None

- [ ] Task 2: `chat_service._detect_and_emit_files` 双后端分支（改 `app/services/chat_service.py`）
  - [ ] 函数签名新增参数 `workspace_manager` 和 `workspace_backend`（从 generate_response 透传）
  - [ ] OpenSandbox 分支：before/after 快照改用 `await workspace_manager.list_session_files(user_id, session_id)`；`build_file_meta` 改用 `await workspace_manager.stat_session_file(...)` 提供 size，`media_type` 仍用 `mimetypes.guess_type(rel_path)`，`url` 仍为 `/files/{session_id}/{rel_path}`
  - [ ] Docker 分支：保持原 `snapshot(os.path.join(WORKSPACE_BASEDIR, session_id))` + `build_file_meta(workdir, ...)` 不变
  - [ ] generate_response 调用处透传 `workspace_manager=app.state.workspace_manager`、`workspace_backend=app.state.workspace_backend`、`user_id`
  - [ ] `files_generated` 事件 payload 与 `append_session_files` 持久化逻辑不变

- [ ] Task 3: `files.py` 下载/内联双后端分支 + 越权校验重写（改 `app/routes/files.py`）
  - [ ] 从 `app.state` 读取 `workspace_manager` 和 `workspace_backend`（`request.app.state`）
  - [ ] 越权校验改为规范化相对路径校验：`rel = path.replace("\\", "/").lstrip("/")`；若 `"/.." in rel` 或 `rel.startswith("..")` 或 `os.path.isabs(path)` → 403
  - [ ] OpenSandbox 下载分支（无 mode 或 mode=download）：`content = await manager.read_session_file(user_id, session_id, rel)`；None → 404；`media_type = mimetypes.guess_type(rel)[0] or "application/octet-stream"`；返回 `Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{basename}"'})`
  - [ ] OpenSandbox inline 分支（mode=inline）：同上读取；文本类（media_type 以 `text/` 开头或 rel 后缀在 md/txt/json/csv/log/代码后缀集合）返回 `text/...; charset=utf-8`；图片类返回对应 image mime；`Content-Disposition: inline`；pdf/docx 等不支持的返回 415
  - [ ] Docker 分支：保持原 `FileResponse(target, ...)` 不变；inline 模式在 Docker 下也支持（读文件字节返回，逻辑与 OpenSandbox inline 一致但读宿主机文件）
  - [ ] 需要 `user_id` 用于 OpenSandbox 管理器定位沙箱（从 `current_user` 依赖获取）

- [ ] Task 4: 语法校验与验证
  - [ ] `python -m py_compile` 通过 3 个修改文件
  - [ ] grep 确认 OpenSandbox 分支仅在 `workspace_backend == "opensandbox"` 时走管理器，Docker 分支保持原逻辑
  - [ ] 核对 Docker 后端默认行为不变（WORKSPACE_BACKEND 未设置时走原宿主机路径）
  - [ ] 核对 `files_generated` 事件结构与 `session_files` 持久化未改变

# Task Dependencies
- Task 2 依赖 Task 1（需管理器 list_session_files/stat_session_file）
- Task 3 独立（与 Task 2 可并行，均依赖 Task 1 的 read_session_file）
- Task 4 依赖 Task 1-3 全部完成
