# 移植制度问答后端（regulation）到当前项目 Spec

## Why

制度问答（policy QA）目前是一个独立部署的后端服务（regulation 仓库，独立 host/端口、独立数据库、独立 JWT 鉴权、独立 Langfuse 项目）。当前项目通过 `policy_qa` 工具远程调用 `POLICY_QA_BASE_URL/api/v1/policy/qa` 获取检索问答结果。现要将该功能移植为当前项目内的一部分，消除远程调用、统一数据库与鉴权，同时合并数据大盘功能。

## What Changes

- **新增自包含模块 `app/regulations/`**：移植 regulation 仓库的 `/api/v1/policy/qa`（blocking + streaming）与数据大盘功能，作为当前项目内部模块，不再独立启动服务。
- **数据库**：`app/dao/init_mysql.py` 新增 `knowledge_gaps` 表（数据大盘知识缺口功能依赖）。`qa_conversations`、`qa_messages`、`qa_suggested_questions` 属于 `/chat/messages` 会话功能，**不移植**（要求 7）。
- **模型配置**：`config/model_config.yml` 新增 `regulations_qa` 模型条目；`model_name`/`base_url`/`api_key` 任一为空或整节缺失时回退 `default` 模型。
- **Langfuse**：制度问答使用独立的公钥/私钥（`REGULATIONS_LANGFUSE_PUBLIC_KEY` / `REGULATIONS_LANGFUSE_SECRET_KEY`），host 复用当前项目 `LANGFUSE_HOST`。移植 regulation 的 Langfuse client（`app/regulations/providers/langfuse.py`），与当前项目 `LangfuseService` 互不干扰。
- **鉴权**：取消 regulation 独立 JWT。`/api/v1/policy/qa` 与 `/dashboard/*` 均使用当前项目统一 JWT（`get_current_user`），user_id / department 从当前项目 JWT payload 获取。**BREAKING**（对原 regulation 前端）：原 `DASHBOARD_INTERNAL_TOKEN` 内部鉴权取消。
- **policy_qa 工具本地化**：`tools/policy_qa_tools.py` 不再 HTTP 调用 `POLICY_QA_BASE_URL`，改为进程内直接调用移植后的 `PolicyQAService.run_general_qa`；`POLICY_QA_BASE_URL` 配置项删除。
- **不移植**（要求 7/8）：`/chat/messages` 会话链路（conversations/messages 路由、conversation_service、message_service、qa_conversations/qa_messages/qa_suggested_questions 表）、停止会话、查询历史、建议问题、标题生成、消息反馈落库；未实现/设计阶段功能不引入。

## Impact

- Affected code:
  - 新增：`app/regulations/**`（graph/providers/services/dao/schemas/config）、`app/routes/policy_qa.py`、`app/routes/dashboard.py`
  - 修改：`app/dao/init_mysql.py`（knowledge_gaps 表）、`app/main.py`（lifespan 初始化 + 路由注册）、`tools/policy_qa_tools.py`（本地调用）、`config/model_config.yml`（regulations_qa 模型）、`.env.example`、`requirements.txt`（langgraph / langchain-openai / langchain-core）
  - 删除配置：`POLICY_QA_BASE_URL`（`app/config.py` 与 `.env.example`）
- 模块边界：`app/regulations/` 自包含，仅通过 `app/routes/` 薄路由层与 `tools/policy_qa_tools.py` 两个入口与主项目交互；主项目现有代码（chat/orchestrator/session 等）不感知其内部实现。

## 架构决策

1. **保留 LangGraph**：regulation 的问答流程（query 理解 → 检索 → 生成，含澄清/拒答分支）以 LangGraph 图实现，原样移植以降低风险。新增依赖 `langgraph`、`langchain-openai`、`langchain-core`。
2. **日志统一**：regulation 用 loguru，移植时统一替换为当前项目 `logging`（`logging.getLogger(__name__)`），不引入 loguru。
3. **DAO 风格统一**：不引入 SQLAlchemy；`knowledge_gaps` 表操作改写为当前项目的 aiomysql DAO 风格（`app/regulations/dao/knowledge_gap_dao.py`，持有 `app.state.mysql_pool` 同一连接池）。
4. **配置归属**：`app/regulations/config.py` 从环境变量读取模块配置（KB 检索地址/token、top_k、query 理解开关、大盘窗口、独立 Langfuse 密钥）；模型配置由 main.py lifespan 从 `model_config.yml` 解析后注入（空则回退 default）。
5. **路由路径**：`/api/v1/policy/qa` 保持原契约（前端无需改）；大盘为 `/dashboard/kb-overview` 与 `/dashboard/knowledge-gaps/resolve`，鉴权从内部 token 改为统一 JWT。
6. **trace 流水号**：移植 `providers/trace.py` 的 contextvar serial 机制，`app_serial_number` 透传 KB 检索与 LLM 请求头。
7. **SSE 契约**：streaming 模式保持原事件序列（message_start → stage → citation → content_block_delta → message_delta → message_stop，含 ping 心跳），`policy_qa` 工具只用 blocking 模式。

## ADDED Requirements

### Requirement: 制度问答接口 /api/v1/policy/qa

系统 SHALL 在当前项目内提供 `POST /api/v1/policy/qa` 接口，统一 JWT 鉴权，接收 `GeneralQARequest`（question、knowledge_base_ids、app_serial_number、enable_query_understanding、response_mode），blocking 返回 `GeneralQAResponse`（answer、citations、app_serial_number、model、created_at），streaming 返回对齐原 SSE 事件契约的流。无状态、不落会话库；出口打 `[policy-qa]` 概要日志；空应答时后台落知识缺口并打 `empty_answer` score。

#### Scenario: blocking 问答成功
- **WHEN** 已授权用户 POST `/api/v1/policy/qa`，`response_mode=blocking`，携带问题与知识库 ID
- **THEN** 返回 200，`answer` 为基于知识库检索生成的回答，`citations` 为引用文档列表

#### Scenario: streaming 问答
- **WHEN** `response_mode=streaming`
- **THEN** 返回 SSE 流，事件序列符合原契约，长空闲期间有心跳 ping

#### Scenario: 空应答记录知识缺口
- **WHEN** 问答结果为空（无检索结果或回答为空）
- **THEN** 后台按 (kb_id, 规范化问题哈希) upsert `knowledge_gaps` 表一条 open 缺口，新缺口异步用 LLM 概括 question_type，不阻塞响应

#### Scenario: 未授权访问
- **WHEN** 未携带或携带无效 JWT
- **THEN** 返回 401，不进入问答流程

### Requirement: 制度问答模型配置

系统 SHALL 在 `config/model_config.yml` 的 `models.regulations_qa` 中配置制度问答专用大模型；该条目缺失或 `model_name`/`base_url`/`api_key` 为空时 SHALL 回退使用 `models.default`。

#### Scenario: 专用模型生效
- **WHEN** `regulations_qa` 条目完整配置
- **THEN** 制度问答的生成、改写、缺口概括 LLM 均使用该模型

#### Scenario: 回退默认模型
- **WHEN** `regulations_qa` 条目为空或缺失
- **THEN** 自动使用 `default` 模型，功能不受影响

### Requirement: 制度问答独立 Langfuse 项目

系统 SHALL 为制度问答提供独立 Langfuse 客户端：host 与主项目一致（`LANGFUSE_HOST`），公钥/私钥单独配置（`REGULATIONS_LANGFUSE_PUBLIC_KEY` / `REGULATIONS_LANGFUSE_SECRET_KEY`）；密钥为空时禁用埋点。主项目 `LangfuseService` 不受影响。

#### Scenario: 双项目并存
- **WHEN** 主项目与制度问答各自的 Langfuse 密钥均已配置
- **THEN** 普通会话 trace 上报到主项目，制度问答 trace（name=kb_search）上报到制度问答项目，互不混淆

### Requirement: 数据大盘接口

系统 SHALL 提供 `GET /dashboard/kb-overview`（一次返回 overview、search_trend、department_distribution、department_usage_ranking、top_cited_knowledge、knowledge_gaps 全部指标）与 `POST /dashboard/knowledge-gaps/resolve`（按 gap_ids 或 kb_id 批量闭合缺口并失效缓存），统一 JWT 鉴权。`REGULATIONS_DASHBOARD_USE_REAL_DATA=false` 返回 Mock；`true` 查询制度问答 Langfuse 项目、KB 大盘接口与 `knowledge_gaps` 表，带 300s 单条缓存，查询失败返回空数据不回退 Mock。

#### Scenario: Mock 模式
- **WHEN** `REGULATIONS_DASHBOARD_USE_REAL_DATA=false`
- **THEN** 返回结构完整的 Mock 数据

#### Scenario: 真实数据模式
- **WHEN** `=true` 且 Langfuse/KB 可达
- **THEN** 返回基于近 N 天（`REGULATIONS_DASHBOARD_LOOKBACK_DAYS`，默认 30）真实 trace 聚合的指标

#### Scenario: 闭合知识缺口
- **WHEN** POST `/dashboard/knowledge-gaps/resolve` 携带 gap_ids
- **THEN** 对应 open 缺口标记 resolved，大盘缓存立即失效

### Requirement: policy_qa 工具本地调用

系统 SHALL 将 `tools/policy_qa_tools.py` 的远程 HTTP 调用替换为进程内调用 `PolicyQAService.run_general_qa`（blocking）；权限名→知识库 ID 映射、Redis 权限读取、引用格式化逻辑保持不变；删除 `POLICY_QA_BASE_URL` 配置。

#### Scenario: 工具调用
- **WHEN** 智能体调用 `policy_qa` 工具且用户具备制度问答权限
- **THEN** 进程内完成检索与生成，返回回答正文与引用来源，无外部 HTTP 调用（KB 检索服务除外）

## REMOVED Requirements

### Requirement: 独立服务部署（regulation）
**Reason**: 不再作为单独 host/端口服务（要求 1）。
**Migration**: 功能并入 `app/regulations/` 模块，由当前项目 `main.py` lifespan 统一初始化。

### Requirement: regulation 独立 JWT 鉴权
**Reason**: 整个项目只有一个 JWT 私钥，签发只在 auth.py（要求 5）。
**Migration**: 所有制度问答/大盘接口改用当前项目 `get_current_user`；regulation 的 `core/security.py` 不移植。

### Requirement: /chat/messages 会话链路
**Reason**: 当前项目已有完整会话体系，暂不合并（要求 7）。
**Migration**: 不移植 conversations/messages 路由、会话/消息/建议问题表、停止会话、标题生成、建议问题、反馈落库。

### Requirement: DASHBOARD_INTERNAL_TOKEN 大盘鉴权
**Reason**: 统一 JWT（用户确认）。
**Migration**: 大盘接口改用 `get_current_user` 依赖。
