# 文件上传与附件对话功能 Spec

## Why

当前系统不支持文件上传，用户无法在对话中发送图片、文档等附件。需要提供文件上传接口，让用户上传文件后能在聊天中引用附件，实现多模态对话体验。同时需要支持用户在首次问答前（尚无 session_id）即可上传文件。

## What Changes

- **新增 `POST /upload` 接口** —— 接收 multipart 文件上传，`session_id` 可选；有则按 session 隔离，无则存到共享 data 目录。返回 `DataBlock`（URLSource）供前端在后续对话中引用
- **修改 `/chat` 接口的消息处理** —— 前端发送的消息 content 支持 `[{"type": "text", "text": "..."}, {"type": "data", ...}]` 混合格式，将其构造为包含 `TextBlock` 和 `DataBlock` 的 `UserMsg`
- **新增 `app/services/file_service.py`** —— 文件存储与 DataBlock 构造服务
- **新增 `app/routes/upload.py`** —— 文件上传路由
- **新增 `app/models/upload.py`** —— 上传响应模型

## Impact

- Affected specs: chat（消息处理逻辑变更）、routes（新增 upload 路由）
- Affected code:
  - `app/services/` —— 新增 `file_service.py`
  - `app/routes/` —— 新增 `upload.py`
  - `app/models/` —— 新增 `upload.py`
  - `app/services/chat_service.py` —— 修改消息处理逻辑
  - `app/main.py` —— 注册新路由
  - `.env.example` —— 新增 `UPLOAD_MAX_SIZE_MB` 配置项
  - `.env` —— 新增 `UPLOAD_MAX_SIZE_MB` 配置项

## ADDED Requirements

### Requirement: 文件上传接口 POST /upload
The system SHALL 提供文件上传接口，接收文件并存储于 workspace 目录。

#### Scenario: 上传成功（有 session_id）
- **WHEN** 前端通过 `multipart/form-data` 上传文件，携带 `session_id`（可选）和 `file` 字段
- **THEN** 系统将文件保存到 `{workdir}/data/{session_id}/{uuid}_{filename}`，返回 DataBlock 格式：

#### Scenario: 上传成功（无 session_id）
- **WHEN** 前端上传文件但未提供 `session_id`（首次问答前）
- **THEN** 系统将文件保存到 `{workdir}/data/{uuid}_{filename}` 共享目录，同样返回 DataBlock 格式
```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "datablock": {
      "type": "data",
      "id": "{block_id}",
      "name": "{original_filename}",
      "source": {
        "type": "url",
        "url": "file://{absolute_path}",
        "media_type": "{mime_type}"
      }
    }
  }
}
```

#### Scenario: 上传文件超过大小限制
- **WHEN** 文件大小超过 `UPLOAD_MAX_SIZE_MB` 配置值
- **THEN** 返回 `{"code": 413, "msg": "文件大小超过限制", "data": {}}`

#### Scenario: 未授权
- **WHEN** 请求无有效 JWT
- **THEN** 返回 401

#### Scenario: 不支持的 media type
- **WHEN** media type 不在白名单中
- **THEN** 返回 `{"code": 415, "msg": "不支持的文件类型", "data": {}}`

### Requirement: 附件对话（多模态 UserMsg）
The system SHALL 支持在 `/chat` 接口的消息中传递文件附件引用。

#### Scenario: 纯文本对话（不变）
- **WHEN** `messages[].content` 是字符串
- **THEN** 保持现有行为，构造 `UserMsg("user", text_str)`

#### Scenario: 文本+附件混合
- **WHEN** `messages[].content` 是数组，包含 `{"type": "text"}` 和 `{"type": "data", "id": "...", "source": {"type": "url", ...}}` 等块
- **THEN** 系统将每个块解析为 `TextBlock` / `DataBlock` 对象，构造出 `UserMsg(name="user", content=[TextBlock(...), DataBlock(...)], role="user")`
- DataBlock 的 source 直接传递 URLSource，不做 base64 解码（文件已在 upload 阶段持久化）

### Requirement: /upload 接口 JWT 校验
The system SHALL 对 `/upload` 接口进行 JWT 鉴权，与 `/chat` 一致。

## MODIFIED Requirements

### Requirement: POST /chat 消息处理（修改）
`generate_response` 函数中的消息处理逻辑需要增强：
- 当 `msg.content` 是 list 时，不再仅提取 text，而是逐 block 解析：
  - `{"type": "text"}` → `TextBlock(text=...)`
  - `{"type": "data"}` → `DataBlock(...)` 直接从 dict 构造（使用 model_validate）
- 构造 `UserMsg(name="user", content=[block1, block2, ...], role="user")`
- 传递给 `agent.reply_stream()`

**注意**：`ContentType` 检查——对于 `ChatRequest.messages` 中的 content，前端传入的 `{"type": "data", "id": "...", "source": {"type": "url", "url": "..."}}` 结构已经和 agentscope 的 DataBlock Pydantic 模型一致，可以直接反序列化。

## REMOVED Requirements
无