# Tasks

- [ ] Task 1: 新增上传配置文件与响应模型
  - 在 `.env` 和 `.env.example` 中添加 `UPLOAD_MAX_SIZE_MB=10`
  - 在 `app/config.py` 中添加 `UPLOAD_MAX_SIZE_MB` 常量和 `UPLOAD_ALLOWED_MEDIA_TYPES` 白名单
  - 创建 `app/models/upload.py`：`UploadResponse`, `UploadErrorResponse`

- [ ] Task 2: 实现文件上传与 DataBlock 构造服务
  - 创建 `app/services/file_service.py`
  - `FileService` 类，依赖 workspace workdir 路径：
    - `save_upload(session_id, filename, content_bytes, media_type)` —— 保存文件到 `{workdir}/data/{session_id}/{uuid}_{filename}`，返回 `DataBlock`（URLSource）
    - `validate_file_size(content_bytes)` —— 校验文件大小
    - `validate_media_type(media_type)` —— 校验媒体类型

- [ ] Task 3: 实现 /upload 路由
  - 创建 `app/routes/upload.py`
  - `POST /upload` —— 接收 `session_id`（可选 form 字段）和 `file`（UploadFile），依赖 JWT 校验
  - 无 session_id 时存到 `{workdir}/data/{uuid}_{filename}`
  - 有 session_id 时存到 `{workdir}/data/{session_id}/{uuid}_{filename}`
  - 校验参数 → 保存文件 → 返回 DataBlock 格式
  - 文件大小/类型校验失败时返回统一错误格式

- [ ] Task 4: 修改 chat_service.py，支持多模态 UserMsg
  - 修改 `generate_response` 中的消息处理逻辑
  - 当 `msg.content` 是 list 时，逐 block 解析：
    - `{"type": "text"}` → `TextBlock(text=...)`
    - `{"type": "data"}` → `DataBlock.model_validate(block)`
  - 构造 `UserMsg(name="user", content=[...], role="user")` 传入 `agent.reply_stream()`

- [ ] Task 5: 注册新路由到 main.py
  - 在 `app/main.py` 中 import 并注册 `upload.router`

- [ ] Task 6: 端到端验证
  - `POST /upload` 上传文件 → 验证返回 DataBlock
  - `POST /upload` 无 session_id → 400
  - `POST /upload` 无 token → 401
  - `POST /chat` 纯文本 → 正常流式回复
  - `POST /chat` 文本+data block → 正常流式回复
  - `POST /upload` 文件超大 → 413

# Task Dependencies
- Task 1~2 可并行
- Task 3 依赖 Task 1, Task 2
- Task 4 无外部依赖（只修改 chat_service.py）
- Task 5 依赖 Task 3
- Task 6 依赖 Task 4, Task 5