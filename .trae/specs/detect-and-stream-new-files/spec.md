# 对话后检测并流式返回新文件 Spec

## Why

智能体在对话中可能生成文件（图表、报表、脚本产物等）落到 session 工作目录。当前前端无感知，用户只能事后手动查看目录。需在每轮 `/chat` 编排结束后自动检测本轮新生成的文件，并通过 SSE 事件把可下载的 HTTP URL 推给前端，让用户即时看到 agent 产物并下载。

## What Changes

- **新增** 文件变更检测服务 `app/services/file_change_detector.py`：提供 `snapshot(workdir)`（递归扫描，排除 `data/` 上传子目录，返回相对路径集合）与 `diff(before, after)`（返回新增相对路径列表）。
- **新增** 文件下载路由 `app/routes/files.py`：`GET /files/{session_id}/{path:path}`，带 `Depends(current_user)` 鉴权 + `realpath` 路径越权校验（确保 path 不逃逸 `{WORKSPACE_BASEDIR}/{session_id}`），返回 `FileResponse`。
- **改** `app/services/chat_service.py` 的 `generate_response`：编排开始前对 session 工作目录做快照；编排 + 持久化完成后扫描并 diff，对每个新文件构造 HTTP 下载 URL，组装 `files_generated` 事件 yield；事件位置在编排结束之后、`trace_ready` 之前。
- **改** `app/main.py`：注册 `files` router。
- **不改** `orchestrator_service.py`、`workspace_manager.py`、`upload.py`、`file_service.py`、`registry.py`（仅消费既有 session 工作目录路径）。

## Impact

- **Affected specs**: `introduce-docker-workspace-manager`（复用 `{WORKSPACE_BASEDIR}/{session_id}` 作为扫描根）、`file-upload-attachment`（下载接口与上传落点同源，但互不依赖）、`multi-turn-session`（每轮独立快照）。
- **Affected code**:
  - 新增 `app/services/file_change_detector.py`
  - 新增 `app/routes/files.py`
  - `app/services/chat_service.py`（generate_response 增加快照/diff/yield）
  - `app/main.py`（注册 router）
- **外部依赖**: 无新增第三方依赖（`mimetypes`/`pathlib`/`os` 均为标准库；`FileResponse` 已在 fastapi 中）。

## ADDED Requirements

### Requirement: 文件变更检测

系统 SHALL 提供文件变更检测能力：在每轮 `/chat` 编排开始前对 session 工作目录 `{WORKSPACE_BASEDIR}/{session_id}` 递归快照（记录所有相对路径），编排结束后再扫一次，diff 出本轮新增的相对路径列表。扫描 SHALL 排除 `data/` 子目录（上传落点，非 agent 产物）。

#### Scenario: 编排过程中 agent 生成新文件
- **WHEN** agent 在本轮编排中向 session 工作目录（顶层或任意非 `data/` 子目录）写入新文件
- **THEN** 编排结束后的 diff 包含该文件的相对路径

#### Scenario: 本轮前已存在的文件不计入
- **WHEN** 某文件在本轮快照前已存在（上一轮生成或本轮前 /upload）
- **THEN** diff 不包含该文件

#### Scenario: data/ 子目录文件不计入
- **WHEN** 本轮 /upload 写入 `{WORKSPACE_BASEDIR}/{session_id}/data/...`
- **THEN** 即使是本轮新增，diff 也不包含（扫描时跳过 `data/`）

#### Scenario: session 工作目录不存在
- **WHEN** 快照时 `{WORKSPACE_BASEDIR}/{session_id}` 不存在（如 ephemeral session 或首次未创建）
- **THEN** 快照返回空集合，不抛异常；diff 结果为编排后扫描到的全部非 `data/` 文件

### Requirement: 新文件 SSE 事件

系统 SHALL 在每轮 `/chat` SSE 流中，编排结束、消息持久化完成之后，`trace_ready` 之前，yield 一个 `files_generated` 事件（即使新文件列表为空也发，payload `files` 为空数组，便于前端统一处理）。

#### Scenario: 有新文件
- **WHEN** diff 得到 N > 0 个新文件
- **THEN** yield `{"type": "files_generated", "files": [{"name": 文件名, "path": 相对路径, "url": "/files/{session_id}/{相对路径}", "size": 字节数, "media_type": 猜测的MIME或"application/octet-stream"}, ...]}`

#### Scenario: 无新文件
- **WHEN** diff 为空
- **THEN** yield `{"type": "files_generated", "files": []}`

#### Scenario: 事件顺序
- **WHEN** 一轮 /chat 完整 SSE 流
- **THEN** 顺序为：`session_ready` → 编排事件（reply 内容等）→ 持久化（无事件）→ `files_generated` → `trace_ready`

### Requirement: 文件下载接口

系统 SHALL 提供 `GET /files/{session_id}/{path:path}` 接口，对登录用户（`Depends(current_user)`）返回该 session 工作目录下指定相对路径的文件内容。

#### Scenario: 合法路径下载
- **WHEN** 登录用户请求 `/files/{session_id}/report.csv`，且该文件真实存在于 `{WORKSPACE_BASEDIR}/{session_id}/report.csv`
- **THEN** 返回 `FileResponse`，带 `Content-Type`（按 `mimetypes.guess_type` 猜测）与 `filename`（basename）

#### Scenario: 路径越权拦截
- **WHEN** 请求 path 含 `../` 或绝对路径片段，使 `realpath` 逃逸出 `{WORKSPACE_BASEDIR}/{session_id}`
- **THEN** 返回 403，不返回文件内容

#### Scenario: 文件不存在
- **WHEN** 请求的文件在 session 目录下不存在
- **THEN** 返回 404

#### Scenario: 未登录访问
- **WHEN** 未携带有效 JWT
- **THEN** 返回 401（由 `current_user` 依赖抛出）

## MODIFIED Requirements

### Requirement: generate_response 事件流

`chat_service.generate_response` SHALL 在 orchestrator run 开始前对 session 工作目录快照；在 orchestrator run + 消息持久化之后、`trace_ready` 之前，扫描并 diff，yield `files_generated` 事件。快照/扫描失败 SHALL 记录 warning 但不阻断主流程（仍发一个空 `files_generated` 事件）。

## Assumptions & Decisions

1. **本轮快照对比**：以 /chat 开始为基线，编排后差集即新文件；覆盖已有文件不计入（用户已选）。
2. **HTTP URL 形式**：新增 `/files/{session_id}/{path}` 下载接口，事件里返回该接口的相对 URL，前端拼接 baseURL 访问（用户已选）。
3. **排除 data/ 子目录**：`/upload` 落点是 `{WORKSPACE_BASEDIR}/{session_id}/data/...`，扫描时跳过 `data/`，避免上传文件被误报（用户已选）。
4. **事件位置**：编排结束 → 持久化 → `files_generated` → `trace_ready`（用户已选）。
5. **空列表也发事件**：前端可统一处理，无需判断事件是否到达。
6. **路径越权校验**：用 `os.path.realpath` 解析后比较 `startswith(base + os.sep)`，防止符号链接 / `../` 逃逸。
7. **media_type 猜测**：`mimetypes.guess_type`，失败回退 `application/octet-stream`。
8. **扫描根**：`{WORKSPACE_BASEDIR}/{session_id}`（与 Docker workspace 的 host_workdir 同路径，与 /upload 落点同路径）。
9. **快照/扫描失败降级**：任何异常记 warning，diff 视为空，仍发空 `files_generated` 事件，不阻断 /chat。
10. **不引入新依赖**：仅用标准库 + fastapi `FileResponse`。
