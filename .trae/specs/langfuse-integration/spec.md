# Langfuse 可观测性集成 Spec

## Why

当前系统缺乏 LLM 调用链路的可观测性。每次智能体对话的输入、输出、技能调用、token 消耗、耗时等关键指标均无记录，导致排障和优化缺乏数据支撑。接入 Langfuse 后可通过 Web UI 可视化 trace 链路、分析成本、收集用户反馈。

## What Changes

- **新增 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_HOST` 配置** —— 写入 `.env` / `.env.example` / `app/config.py`
- **新增 `app/services/langfuse_service.py`** —— Langfuse 客户端初始化及工具方法，非强依赖（Langfuse 不可用时系统不受影响）
- **修改 `app/services/chat_service.py`** —— 在 `generate_response` 中集成 Langfuse trace，按 session_id 分组
- **修改 `app/routes/chat.py`** —— 在 SSE 流中新增 `TRACE_READY` 事件，返回 trace_id
- **修改 `requirements.txt`** —— 新增 `langfuse` 依赖

## Impact

- Affected specs: chat（对话流增加 trace 事件）、config（新增 langfuse 配置项）
- Affected code:
  - `app/services/` —— 新增 `langfuse_service.py`
  - `app/services/chat_service.py` —— 修改 `generate_response`
  - `app/routes/chat.py` —— 新增 `TRACE_READY` SSE 事件
  - `app/config.py` —— 新增 LANGFUSE 配置常量
  - `.env` / `.env.example` —— 新增 Langfuse 配置项
  - `requirements.txt` —— 新增 `langfuse`

## ADDED Requirements

### Requirement: Langfuse 配置

The system SHALL 支持通过环境变量配置 Langfuse 连接信息。

#### Scenario: 配置项已设置
- **WHEN** `.env` 文件中配置了 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_HOST`
- **THEN** 系统初始化 Langfuse 客户端，启用追踪

#### Scenario: 配置项缺失
- **WHEN** 未配置 Langfuse 相关环境变量
- **THEN** 系统静默跳过 Langfuse 初始化，所有追踪方法变为空操作（no-op），不影响正常对话流程

### Requirement: 对话 Trace 追踪

The system SHALL 对每一轮对话生成一个 Langfuse trace，包含输入、输出、耗时、token 消耗和技能工具调用信息。

#### Scenario: 正常对话完成
- **WHEN** 一轮对话完成（`generate_response` 执行完毕）
- **THEN** 系统创建 Langfuse trace：
  - Trace 名称: `chat-response`
  - `session_id`：当前对话 session_id，用于 Langfuse 会话分组
  - `user_id`：当前用户 ID
  - `input`：用户发送的 messages 列表
  - `output`：assistant 回复消息的完整内容
  - token 使用量（从 `apply.usage` 提取）
  - 技能工具调用列表（从 `apply.content` 中的 `ToolCallBlock` 提取）

#### Scenario: 对话异常中断
- **WHEN** 对话过程中抛出异常
- **THEN** trace 的 `output` 记录错误信息，trace 仍保留在 Langfuse 中用于排障

### Requirement: 前端接收 trace_id

The system SHALL 在 SSE 流中返回 `trace_id`，供前端后续收集用户反馈。

#### Scenario: SSE 流结束前发送 trace_id
- **WHEN** 对话流式回复结束，generator 即将结束
- **THEN** 在 SSE 流的末尾发送一个 `TRACE_READY` 事件：
  ```json
  data: {"type": "trace_ready", "trace_id": "lf-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
  ```
  如果 Langfuse 未启用则 `trace_id` 为 `null`。

### Requirement: Langfuse 非强依赖

The system SHALL 确保 Langfuse 不可用时后端系统不受影响。

#### Scenario: Langfuse 服务不可达
- **WHEN** Langfuse 服务器无法连接或初始化失败
- **THEN** 系统捕获异常，将 `_enabled` 标记为 `False`，后续所有追踪调用变为空操作，对话服务正常运行

#### Scenario: Langfuse 追踪调用异常
- **WHEN** `trace.update()` 或 `trace()` 等调用抛出异常
- **THEN** 系统在 `langfuse_service` 内部捕获异常并记录日志，不向上层传播

## MODIFIED Requirements

### Requirement: POST /chat SSE 事件流（修改）

在 `StreamingResponse` 的 generator 末尾增加一个 `TRACE_READY` 事件，格式：
```json
data: {"type": "trace_ready", "trace_id": "lf-xxx..."}
```

### Requirement: generate_response 加入 Langfuse 追踪（修改）

在 `generate_response` 函数中：
- 方法开始时通过 `langfuse_service` 创建一个 trace（若已启用）
- 设置 `session_id`、`user_id`、`input`
- 在流结束、持久化状态之后：
  - 从 `apply.usage` 提取 token 使用量附加到 trace
  - 从 `apply.content` 提取 `ToolCallBlock` 列表作为工具调用信息
  - 设置 `output` 为 apply 的 JSON dump
  - 调用 `langfuse.flush()` 确保事件发送
- 返回 trace_id 供 chat route 发送 TRACE_READY 事件

## REMOVED Requirements

无