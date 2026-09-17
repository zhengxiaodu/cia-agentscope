# Checklist

## 模块结构与边界
- [x] `app/regulations/` 自包含：graph/providers/services/dao/schemas/config 全部位于模块内，模块外仅 `app/routes/policy_qa.py`、`app/routes/dashboard.py`、`tools/policy_qa_tools.py`、`app/main.py` 接线处引用
- [x] 未移植项确认：无 qa_conversations/qa_messages/qa_suggested_questions 表；无 conversations/messages 路由；无 conversation_service/message_service；无标题生成/建议问题/停止会话/反馈落库；无 DASHBOARD_INTERNAL_TOKEN；无 regulation 独立 JWT 代码（core/security.py）
- [x] 未引入 sqlalchemy、loguru、pydantic-settings 依赖；日志统一用 logging

## 服务形态
- [x] 无独立 host/端口/main 启动入口；服务由当前项目 lifespan 初始化并挂 app.state
- [x] 共用当前项目 MySQL 连接池（app.state.mysql_pool），无独立数据库连接配置

## 数据库
- [x] `knowledge_gaps` 表已加入 init_mysql.py 且幂等建表（重复启动不报错）
- [x] 表结构与原 schema.sql 等价（字段/唯一键/索引）

## 模型配置
- [x] model_config.yml 存在 models.regulations_qa 条目
- [x] 条目完整时制度问答使用专用模型；缺失/关键字段为空时回退 models.default（有测试覆盖）
- [x] 生成/改写/缺口概括 LLM 均来自同一模型解析结果

## Langfuse
- [x] 制度问答 trace 使用 REGULATIONS_LANGFUSE_PUBLIC_KEY/SECRET_KEY，host 复用 LANGFUSE_HOST
- [x] 密钥为空时埋点全部 no-op，不影响问答功能
- [x] 主项目 LangfuseService 行为不变（现有测试通过）
- [x] trace name=kb_search、retrieval span、empty_answer score 上报逻辑与原实现一致

## 鉴权
- [x] /api/v1/policy/qa 与 /dashboard/* 均走 get_current_user；无 JWT 返回 401
- [x] 全项目 JWT 签发仍只在 app/routes/auth.py；未新增任何 create token 代码
- [x] user_id/department 从当前项目 JWT payload 取值并透传 KB 检索与 Langfuse 埋点

## /api/v1/policy/qa 接口
- [x] 请求/响应 schema 与原 GeneralQARequest/GeneralQAResponse 契约一致
- [x] blocking：返回 answer + citations + app_serial_number + model + created_at
- [x] streaming：SSE 事件序列 message_start → stage → citation → content_block_delta → message_delta → message_stop，长空闲有 ping 心跳，空检索补发最终答案
- [x] 无状态不落库；出口 [policy-qa] 概要日志（不含正文）
- [x] 空应答：后台 upsert knowledge_gaps + LLM 概括 question_type，不阻塞响应

## 数据大盘
- [x] GET /dashboard/kb-overview 返回完整 DashboardData 结构（六块指标）
- [x] REGULATIONS_DASHBOARD_USE_REAL_DATA=false 走 Mock；true 走真实聚合，失败返回空数据不回退 Mock
- [x] 300s 单条缓存；POST /dashboard/knowledge-gaps/resolve 后缓存立即失效
- [x] 真实模式：Langfuse v1 API 查询用制度问答密钥；拒答 trace 不计入检索指标；P95 只统计 kb_search 父 trace 的 retrieval span

## policy_qa 工具
- [x] 工具内无 POLICY_QA_BASE_URL 引用、无对 /api/v1/policy/qa 的 HTTP 自调用（进程内直调）
- [x] 权限名→知识库 ID 映射、Redis 权限读取、无权限文案、引用格式化、ToolChunk metadata 契约与原实现一致
- [x] KB 检索失败/超时有兜底文案，不抛异常打断智能体

## 配置清理
- [x] app/config.py 与 .env.example 已删除 POLICY_QA_BASE_URL
- [x] .env.example 新增 REGULATIONS_* 配置段并含注释说明（Langfuse 密钥独立、KB API、大盘开关/窗口、top_k 覆盖、query 理解开关）
- [x] requirements.txt 新增 langgraph、langchain-openai、langchain-core 且 pip 可安装

## 测试与回归
- [x] 新增单元测试全部通过（service/dao/dashboard/config 回退/工具本地化）
- [x] 现有 pytest 套件全量回归通过（尤其 test_agent_access_description、test_sensitive_service、langfuse 相关）
- [x] 应用可正常启动（lifespan 无初始化报错），/health 正常
