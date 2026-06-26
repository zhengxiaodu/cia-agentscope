# Tasks

- [x] Task 1: 新增 Langfuse 配置项
  - 在 `.env` 和 `.env.example` 中添加 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_HOST`
  - 在 `app/config.py` 中添加对应的常量和环境变量读取
  - 在 `requirements.txt` 中添加 `langfuse`

- [x] Task 2: 实现 LangfuseService
  - 创建 `app/services/langfuse_service.py`
  - `LangfuseService` 类：
    - 从配置读取 LANGFUSE 凭证，初始化 `langfuse.Langfuse` 客户端
    - 凭证缺失或初始化失败时 `enabled=False`，所有方法变空操作
    - 提供 `create_trace()` / `update_trace()` / `flush()` 方法
    - 所有与 Langfuse 的交互均被 try/except 包裹

- [x] Task 3: 修改 chat_service.py 集成 Langfuse 追踪
  - 在 `generate_response` 中：
    - 函数开头创建 trace（`langfuse_service.create_trace(...)`）
    - 设置 session_id、user_id、input
    - 函数结束后从 `apply.usage` 和 `apply.content` 提取 token 用量和工具调用信息
    - 设置 trace 的 output
    - 调用 `langfuse.flush()`
    - 通过 generator 向 SS E 末尾 yield TRACE_READY 事件

- [x] Task 4: 修改 chat.py/main.py 传递 LangfuseService
  - `main.py` lifespan 中初始化 `LangfuseService` 并存入 `app.state.langfuse_service`
  - `chat.py` 将 `app.state.langfuse_service` 传入 `generate_response`

- [x] Task 5: 端到端验证
  - 配置完整时 /chat 返回 TRACE_READY 事件
  - 配置缺失时 /chat 正常运行，TRACE_READY 中 trace_id 为 null
  - Langfuse 服务不可达时系统不崩溃

# Task Dependencies
- Task 1 为 Task 2 的前置
- Task 2 为 Task 3 的前置
- Task 3 为 Task 4 的前置
- Task 5 依赖 Task 1~4