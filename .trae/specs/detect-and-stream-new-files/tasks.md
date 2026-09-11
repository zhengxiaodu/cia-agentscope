# Tasks

- [x] Task 1: 实现文件变更检测服务（新文件 `app/services/file_change_detector.py`）
  - [x] `snapshot(workdir: str) -> set[str]`：递归扫描 `workdir`，返回相对路径集合（POSIX 风格 `/` 分隔）；跳过名为 `data` 的顶层子目录；目录不存在返回空集合，不抛异常
  - [x] `diff(before: set[str], after: set[str]) -> list[str]`：返回 `after - before` 的新相对路径列表（按路径排序，保证事件顺序稳定）
  - [x] `build_file_meta(workdir: str, rel_path: str, session_id: str) -> dict`：返回 `{"name": basename, "path": rel_path, "url": f"/files/{session_id}/{rel_path}", "size": 字节数, "media_type": mimetypes.guess_type 或 "application/octet-stream"}`；文件不存在返回 None
  - [x] 单测自检：snapshot 排除 data/、diff 顺序稳定、build_file_meta 字段齐全

- [x] Task 2: 实现文件下载路由（新文件 `app/routes/files.py`）
  - [x] `GET /files/{session_id}/{path:path}`，`user: dict = Depends(current_user)`
  - [x] 计算 `base = os.path.realpath(os.path.join(WORKSPACE_BASEDIR, session_id))`、`target = os.path.realpath(os.path.join(base, path))`
  - [x] 越权校验：`target != base` 且非 `target.startswith(base + os.sep)` → 返回 403
  - [x] 非文件或不存在 → 404
  - [x] 返回 `FileResponse(target, filename=basename, media_type=mimetypes.guess_type 或 "application/octet-stream")`
  - [x] 从 `app.config` 导入 `WORKSPACE_BASEDIR`；从 `app.dependencies` 导入 `current_user`

- [x] Task 3: 改造 `app/services/chat_service.py` 的 `generate_response` 接入检测 + 事件
  - [x] 顶部新增 `from app.config import WORKSPACE_BASEDIR`、`from app.services.file_change_detector import snapshot, diff, build_file_meta`、`import os`
  - [x] 在 `async for event_str in orchestrator_service.run(...)` 之前：`before = snapshot(os.path.join(WORKSPACE_BASEDIR, session_id)) if session_id else set()`（包 try/except，失败 `before=set()` 并记 warning）
  - [x] 在消息持久化完成之后、`trace_ready` 之前：`after = snapshot(...)`（同样 try/except）→ `new_files = diff(before, after)` → 对每个 rel_path 调 `build_file_meta` 收集非 None 结果 → yield `{"type": "files_generated", "files": [...]}`（即使空也发）
  - [x] 整个检测/diff/构造/yield 包在 try/except 里：任何异常记 warning，仍 yield 空 `files_generated` 事件，不阻断后续 `trace_ready`

- [x] Task 4: 在 `app/main.py` 注册 files router
  - [x] `from app.routes import auth, chat, feedback, files, health, mng_proxy, sessions, upload`
  - [x] `app.include_router(files.router, tags=["files"])`

- [x] Task 5: 端到端静态核对
  - [x] `python -m py_compile` 通过新增/修改的 4 个文件
  - [x] grep 确认 `files_generated` 事件仅在 `chat_service.py` yield；`/files/` 路由仅在 `files.py` 注册
  - [x] 核对 checklist 全部条目

# Task Dependencies
- Task 3 依赖 Task 1（需 snapshot/diff/build_file_meta）
- Task 4 与 Task 2 可并行（互不依赖）
- Task 5 依赖 Task 1-4 全部完成
