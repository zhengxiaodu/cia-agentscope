# 持久化并在会话详情中返回生成的文件 Spec

## Why

智能体在每轮 `/chat` 结束时会通过 `chat_service.generate_response` 流式 yield 一个 `files_generated` 事件，记录本轮新生成的文件（name/path/url/size/media_type）。但该事件是瞬时的，文件元信息**未被持久化**，导致后续调用 `GET /sessions/{session_id}` 获取会话详情历史时无法看到之前各轮生成的文件。需要把每轮生成的文件元信息落库，并在会话详情接口中一并返回，让用户回看历史时能重新看到并下载这些 agent 产物。

## What Changes

- **新增** MySQL 表 `session_files`：持久化每个 session 下生成的文件元信息（按 `(session_id, path)` 去重 UPSERT）。在 `app/dao/init_mysql.py` 的 `INIT_SQL` 中追加建表与索引语句。
- **新增** MySQL DAO 方法（`app/dao/mysql_session_dao.py`）：`append_session_files(session_id, files)`（UPSERT）与 `load_session_files(session_id)`（按首次生成时间排序返回）。
- **新增** `SessionService` 方法（`app/services/session_service.py`）：`append_session_files` / `load_session_files`，转调 DAO。
- **改** `app/services/session_service.py` 的 `get_session_detail`：在加载消息后，加载该 session 的文件列表并填入响应。
- **改** `app/services/chat_service.py` 的 `generate_response`：在 yield `files_generated` 事件之后，若 `files_payload` 非空且 `session_service`/`session_id` 可用，调用 `session_service.append_session_files` 落库；失败记 warning，不阻断主流程。
- **改** `app/models/session.py`：新增 `SessionFile` 模型；`SessionDetailResponse` 新增 `files: List[SessionFile]` 字段（默认空列表，向后兼容）。
- **不改** `app/dao/session_dao.py`（Redis DAO 未在 `main.py` 中启用，保持现状）、`app/routes/files.py`（下载接口已存在）、`app/routes/sessions.py`（响应序列化已通过 `model_dump`）。

## Impact

- **Affected specs**: `detect-and-stream-new-files`（复用其 `files_generated` 事件 payload 与 `build_file_meta` 产出的字段，本变更将其落库）、`multi-turn-session`（每轮独立 UPSERT，跨轮累积文件列表）。
- **Affected code**:
  - `app/dao/init_mysql.py`（追加 `session_files` 建表 SQL）
  - `app/dao/mysql_session_dao.py`（新增两个方法）
  - `app/services/session_service.py`（新增两个方法 + 改 `get_session_detail`）
  - `app/services/chat_service.py`（`generate_response` 末尾追加落库调用）
  - `app/models/session.py`（新增 `SessionFile` + `SessionDetailResponse.files`）
- **外部依赖**: 无新增第三方依赖（仅复用现有 `aiomysql` / `pydantic`）。

## ADDED Requirements

### Requirement: 生成文件元信息持久化

系统 SHALL 在每轮 `/chat` 生成 `files_generated` 事件后，将该轮非空的文件元信息列表持久化到 MySQL `session_files` 表中。持久化以 `(session_id, path)` 为唯一键做 UPSERT：新增则插入（`created_at` 记为首次生成时间），已存在则更新 `name`/`url`/`size`/`media_type`/`updated_at`（`created_at` 保留首次值）。持久化失败 SHALL 仅记录 warning，不阻断 `trace_ready` 等后续事件与主流程。

#### Scenario: 本轮有新文件
- **WHEN** `files_generated` 事件的 `files` 列表非空（N > 0）
- **THEN** 系统对每个文件元信息按 `(session_id, path)` UPSERT 到 `session_files` 表

#### Scenario: 本轮无新文件
- **WHEN** `files_generated` 事件的 `files` 列表为空
- **THEN** 系统不写库（不调用 `append_session_files`）

#### Scenario: 跨轮同一路径被覆盖
- **WHEN** 某文件路径在本轮之前已生成，本轮再次生成（内容/大小可能变化）
- **THEN** `session_files` 中该 `(session_id, path)` 行被 UPDATE，`size`/`media_type`/`updated_at` 更新为最新，`created_at` 保持首次生成时间；最终文件列表中该路径仅出现一次

#### Scenario: 持久化失败降级
- **WHEN** `append_session_files` 抛出异常
- **THEN** 记录 warning，`generate_response` 继续执行后续 `trace_ready` 事件，不向上抛错

### Requirement: 会话详情返回生成文件列表

系统 SHALL 在 `GET /sessions/{session_id}` 的响应中，于现有 `messages` 之外，额外返回 `files` 字段：该 session 历史所有生成文件的元信息列表（按首次生成时间 `created_at` 升序）。文件列表来源于 `session_files` 表，与磁盘当前是否存在该文件无关（即使文件被工作区清理服务回收，历史记录仍可见；实际下载若文件已不存在则由 `/files/{session_id}/{path}` 返回 404）。

#### Scenario: 会话有历史生成文件
- **WHEN** 用户请求其拥有的 session 详情，且该 session 在 `session_files` 表中有记录
- **THEN** 响应 `files` 字段返回 `[{name, path, url, size, media_type, created_at}, ...]`，按 `created_at` 升序

#### Scenario: 会话无历史生成文件
- **WHEN** 该 session 在 `session_files` 表中无记录
- **THEN** 响应 `files` 字段为空数组 `[]`

#### Scenario: 非会话拥有者
- **WHEN** 请求的 session 不属于当前用户
- **THEN** 返回 403（沿用既有 `PermissionError` 处理，不泄露文件列表）

#### Scenario: 会话不存在
- **WHEN** 请求的 `session_id` 在 `sessions` 表中不存在
- **THEN** 返回 404（沿用既有逻辑）

## MODIFIED Requirements

### Requirement: generate_response 事件流

`chat_service.generate_response` SHALL 在 yield `files_generated` 事件之后、yield `trace_ready` 之前，若 `files_payload` 非空且 `session_service` 与 `session_id` 均可用，调用 `session_service.append_session_files(session_id, files_payload)` 将文件元信息落库。该调用 SHALL 包裹在 try/except 中：异常仅记 warning，不影响后续事件。

### Requirement: SessionDetailResponse 结构

`SessionDetailResponse` SHALL 新增 `files: List[SessionFile]` 字段（默认空列表）。`SessionFile` 模型字段为 `name: str`、`path: str`、`url: str`、`size: int`、`media_type: str`、`created_at: Optional[str]`。`get_session_detail` SHALL 在权限校验通过后加载 `session_files` 并填充该字段。

## Assumptions & Decisions

1. **持久化方案**：新建 `session_files` 表存储元信息（而非在 `get_session_detail` 时扫描磁盘）。原因：用户需要"之前产生的文件"的历史记录；工作区有定时清理（`WORKSPACE_RETENTION_DAYS`，默认 7 天），磁盘扫描在清理后会把历史文件丢失；落库可保留完整历史且能区分 agent 产物（`data/` 上传目录的文件本就不会进 `files_generated`）。
2. **去重策略**：以 `(session_id, path)` 为唯一键 UPSERT，同一路径跨轮覆盖只保留一行，`created_at` 记首次、`updated_at` 记最近；保证文件列表无重复且排序稳定。
3. **排序**：`load_session_files` 按 `id`（等价于首次生成顺序）升序返回。
4. **失败降级**：落库失败不阻断 `/chat` 主流程，与既有"快照/扫描失败降级"策略一致。
5. **不修改 Redis DAO**：`app/dao/session_dao.py` 未在 `main.py` 启用，保持现状以控制改动范围。
6. **不修改下载接口**：`/files/{session_id}/{path}` 已存在且带鉴权 + 越权校验，前端用响应中的 `url` 直接访问即可。
7. **向后兼容**：`SessionDetailResponse.files` 默认空列表，旧客户端不受影响。
8. **不引入新依赖**：仅复用 `aiomysql`、`pydantic`。
