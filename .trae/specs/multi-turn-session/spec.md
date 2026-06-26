# 多轮对话与上下文短期记忆（Redis 持久化） Spec

## Why

当前系统每次 `/chat` 调用都创建一个全新的 Agent，无状态无记忆，无法进行多轮对话。前端虽然可以传 `session_id`，但后端不存储任何上下文，每次调用都是孤立请求。需要引入 Redis 会话存储实现短期记忆，让 Agent 在同一个 session 内记住历史对话。

## What Changes

- **新增 Redis 连接配置** 到 `.env` / `app/config.py` —— `REDIS_URL`, `REDIS_SESSION_TTL`
- **新增 `app/dao/session_dao.py`** —— Redis 会话持久化层，封装 Session CRUD 操作
- **新增 `app/services/session_service.py`** —— 会话生命周期管理（创建/加载/保存/查询）
- **修改 `app/services/chat_service.py`** —— 集成 Agent state 的保存与加载
- **修改 `app/routes/chat.py`** —— session_id 自动生成/检测/返回；流结束时持久化
- **新增 `app/routes/sessions.py`** —— `GET /sessions` 和 `GET /sessions/{session_id}` 接口
- **修改 `app/main.py`** —— 注册新的 session 路由
- **修改 `app/models/`** —— 新增会话相关的 Pydantic 模型
- **新增 `.env` 配置项** —— `REDIS_URL`, `REDIS_SESSION_TTL`

## Impact

- Affected specs: auth（新接口共用 JWT 校验）、chat（修改流式处理流程）
- Affected code:
  - `app/config.py` —— 新增 Redis 配置常量
  - `app/dao/` —— 新增 `session_dao.py`
  - `app/services/` —— 新增 `session_service.py`；修改 `chat_service.py`
  - `app/routes/` —— 新增 `sessions.py`；修改 `chat.py`
  - `app/models/` —— 新增 `session.py`
  - `app/main.py` —— 注册新路由
  - `.env` —— 新增 Redis 配置项

## ADDED Requirements

### Requirement: Redis 会话持久化
The system SHALL persist Agent 状态到 Redis，实现 session 级别的短期记忆。

#### Scenario: 会话不存在（新会话）
- **WHEN** 前端调用 `POST /chat`，`session_id` 为空或未在 Redis 中找到
- **THEN** 后端自动生成 UUID session_id，创建全新 Agent，并在流式响应开始前通过 SSE 事件 `SESSION_READY` 将 session_id 发送给前端

#### Scenario: 会话已存在（续聊）
- **WHEN** 前端调用 `POST /chat`，`session_id` 在 Redis 中存在
- **THEN** 后端从 Redis 加载 Agent 状态（`agent.load_state_dict()`），恢复记忆上下文，继续多轮对话

#### Scenario: 流结束后持久化
- **WHEN** 一次流式回复完成（`ReplyEndEvent` 发出后）
- **THEN** 后端调用 `agent.state_dict()` 获取当前完整状态，写入 Redis，并更新会话元信息（最后活跃时间）

### Requirement: GET /sessions 会话列表
The system SHALL 提供接口让前端获取当前用户的会话列表。

- **JWT 校验**：通过 `Authorization: Bearer <token>` 鉴权
- **返回逻辑**：从 Redis 的 `user:{user_id}:sessions` sorted set 中取最近 15 条 session_id，再批量查询 `session_meta:{session_id}` 元信息
- **返回字段**：`[{session_id, created_at, updated_at, message_count, last_summary}]`

#### Scenario: 正常返回
- **WHEN** 用户携带有效 JWT 调用 `GET /sessions`
- **THEN** 返回该用户最新的 15 个会话摘要列表

#### Scenario: 无会话
- **WHEN** 用户无历史会话
- **THEN** 返回空数组 `[]`

### Requirement: GET /sessions/{session_id} 会话详情
The system SHALL 提供接口返回指定 session 的完整对话历史。

- **JWT 校验**：通过 `Authorization: Bearer <token>` 鉴权
- **数据来源**：从 Redis 加载 `session:{session_id}` 对应的 agent state，从 `memory.content` 中提取用户消息和助手回复

#### Scenario: 正常返回
- **WHEN** `session_id` 存在且属于当前用户
- **THEN** 返回 `{session_id, created_at, updated_at, messages: [{role, content, timestamp}]}`，每轮对话包含用户提问和模型最终输出

#### Scenario: 会话不存在
- **WHEN** `session_id` 在 Redis 中不存在
- **THEN** 返回 404

#### Scenario: 会话不属于当前用户
- **WHEN** `session_id` 存在但 `user_id` 不匹配
- **THEN** 返回 403

## MODIFIED Requirements

### Requirement: POST /chat（修改）
- `ChatRequest` 模型不变（已有 `session_id: Optional[str] = None`）
- `ChatResponse` 模型不变
- 流式 SSE 事件新增 `SESSION_READY` 类型，携带 `{"type": "session_ready", "session_id": "xxx"}`
- 内部逻辑改为：prepare session → load state → stream reply → save state
- `user_id` 来源保持从 JWT 解析

## REMOVED Requirements
无