# Tasks

- [x] Task 1: 新增 Redis 配置和环境变量
  - 在 `.env.example` 和 `.env` 中添加 `REDIS_URL` 和 `REDIS_SESSION_TTL`
  - 在 `app/config.py` 中读取并导出 Redis 连接串和 TTL 常量

- [x] Task 2: 新增会话相关 Pydantic 模型
  - 创建 `app/models/session.py`：`SessionMeta`, `SessionListResponse`, `SessionDetailResponse`, `SessionMessage`
  - 更新 `app/models/__init__.py`

- [x] Task 3: 实现 Redis 会话持久化 DAO 层
  - 创建 `app/dao/session_dao.py`
  - `SessionDAO` 类，依赖 Redis 连接，提供：
    - `save_agent_state(session_id, user_id, state_dict)` —— 保存 agent state + 更新 sorted set
    - `load_agent_state(session_id)` —— 加载 agent state dict
    - `get_session_meta(session_id)` —— 获取会话元信息
    - `list_user_sessions(user_id, limit=15)` —— 列出用户会话列表
    - `session_exists(session_id)` —— 检查 session 是否存在
    - `extract_messages_from_state(state_dict)` —— 从 state dict 提取对话历史

- [x] Task 4: 实现会话生命周期管理 Service 层
  - 创建 `app/services/session_service.py`
  - `SessionService` 类，依赖 `SessionDAO`，提供：
    - `get_or_create_session(session_id, user_id)` —— 若无 session_id 或不存在则创建新会话，否则返回已有 session_id
    - `load_agent(agent, session_id)` —— 调用 `agent.load_state_dict()` 从 Redis 恢复状态
    - `save_agent(agent, session_id, user_id)` —— 调用 `agent.state_dict()` 并持久化到 Redis
    - `get_session_detail(session_id)` —— 返回完整对话历史
  - 在 `app/main.py` 的 lifespan 中初始化 `SessionService`（含 Redis 连接），存入 `app.state`

- [x] Task 5: 修改 chat_service.py 适配 session 状态管理
  - `generate_response` 签名不变，保持参数传递 `toolkit`, `model_config` 等
  - 但内部逻辑改为：在创建 Agent 后，通过 `session_service.load_agent(agent, session_id)` 恢复状态
  - 修改 `reply_stream` 循环：在 `ReplyEndEvent` 后调用 `session_service.save_agent(agent, session_id, user_id)`

- [x] Task 6: 修改 /chat 路由，集成 session 生命周期
  - `POST /chat` 路由逻辑改为：
    1. `session_service.get_or_create_session(body.session_id, user.user_id)` 获取/创建 session_id
    2. 将 session_id 和 session_service 传给 `generate_response`
    3. 在 SSE 流开始前先 yield `SESSION_READY` 事件
  - 更新 `app/routes/chat.py`

- [x] Task 7: 新增 /sessions 和 /sessions/{session_id} 路由
  - 创建 `app/routes/sessions.py`
  - `GET /sessions` —— JWT 校验 → 调用 `session_service.list_user_sessions()`
  - `GET /sessions/{session_id}` —— JWT 校验 → 校验归属 → 调用 `session_service.get_session_detail()`
  - 更新 `app/main.py` 注册新路由

- [x] Task 8: 验证端到端功能
  - `POST /login` 获取 token
  - `POST /chat` 第一次（无 session_id）→ 验证返回 SESSION_READY 事件包含新 session_id
  - `POST /chat` 第二次（带上 session_id）→ 验证使用已有会话
  - `GET /sessions` → 验证返回列表
  - `GET /sessions/{session_id}` → 验证返回对话历史
  - 验证未登录/过期 token 访问 /sessions 接口被拦截

# Task Dependencies
- Task 1~4 可并行开发
- Task 5 依赖 Task 4
- Task 6 依赖 Task 4, Task 5
- Task 7 依赖 Task 4
- Task 8 依赖 Task 6, Task 7