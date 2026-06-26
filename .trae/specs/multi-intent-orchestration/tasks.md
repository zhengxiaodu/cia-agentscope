# Tasks

- [x] Task 1: 配置文件与常量
  - 新增 `config/agent_config.yml`：3 个智能体定义（id/name/skills/system_prompt）
  - 新增 `config/intent_config.yml`：意图定义 + 意图→智能体映射 + 守护意图 + 默认编排策略 + 编排器参数
  - 扩展 `config/model_config.yml`：新增 `models.intent_recognizer` 段 + `prompts` 段（rewrite/intent_recognition/react_think）
  - `app/config.py` 新增 `AGENT_CONFIG_PATH` / `INTENT_CONFIG_PATH` 常量

- [x] Task 2: 智能体层 `app/agents/`
  - `base.py`：`AgentDefinition` 数据类
  - `registry.py`：`AgentRegistry` + `load_agent_definitions` + `load_all_skills`，按 skill 子集组装独立 Toolkit，缓存并创建 Agent
  - `factory.py`：`AgentFactory` 意图→智能体门面，含 `create_fallback` 兜底

- [x] Task 3: 意图识别层 `app/intent/`
  - `models.py`：`Intent` / `IntentResult` / `IntentConfig` / `GuardIntentConfig` / `GuardResult`
  - `llm_client.py`：`create_async_client` / `chat_complete` / `extract_json`（非流式 LLM + JSON 提取）
  - `rewriter.py`：`QueryRewriter.rewrite`（联系上下文改写，无上下文跳过）
  - `recognizer.py`：`IntentRecognizer.recognize` + `load_intent_config` + `get_orchestration_mode`（含降级兜底）

- [x] Task 4: 编排层 `app/orchestrator/`
  - `base.py`：`TaskResult` + `BaseOrchestrator`（`_run_single_agent` 共享执行 + `_event` 序列化）
  - `guard.py`：`GuardExecutor.check_all` + `_evaluate_guard`（关键词匹配 + risk_level 拦截/警告）
  - `parallel.py`：`ParallelOrchestrator`（`asyncio.gather` 并行 + 超时 + 汇总）
  - `pipeline.py`：`PipelineOrchestrator`（串行 + 每步守护检查 + 失败拦截）
  - `react.py`：`ReActOrchestrator`（Thought→Act→Observe 循环 + MAX_STEPS 兜底 + 提前终止）

- [x] Task 5: 编排服务 `app/services/orchestrator_service.py`
  - `OrchestratorService.create` 工厂方法：加载智能体 + skill + 意图识别器 + 守护执行器 + 编排器
  - `run` 主流程：提取用户输入/历史 → 改写 → 识别 → 选择编排器 → 执行
  - `_select_orchestrator`：relation → parallel/pipeline/react
  - 懒加载缓存三个编排器实例

- [x] Task 6: 适配现有架构
  - 重构 `app/services/chat_service.py`：`generate_response` 转调 `orchestrator_service.run()`，保留 session 持久化 + Langfuse + CUSTOM_COMPONENT 检测
  - 改 `app/routes/chat.py`：参数从 toolkit/model_config 改为 orchestrator_service
  - 改 `app/routes/health.py`：`/skills` → `/agents`，从 registry 取智能体列表
  - 改 `app/main.py`：lifespan 用 `OrchestratorService.create()` 替代 `load_skills()`
  - 扩展 `app/dao/session_dao.py` + `app/services/session_service.py`：新增 `load_messages` / `append_messages`

- [x] Task 7: 验证
  - 全部新增/修改文件 AST 语法检查通过
  - 三个配置文件解析正确（智能体/意图/守护/编排策略）
  - 意图识别逻辑测试通过：单意图/多意图并行/流水线/ReAct 模式选择、未知意图降级、空意图兜底、JSON 提取
  - 守护执行器测试通过：高风险拦截、中风险警告不拦截、未命中放行
  - 注：agentscope 集成层受环境预存的 sqlalchemy 版本不兼容阻断（`async_sessionmaker` ImportError），非本次改动引入，需 `pip install --upgrade sqlalchemy` 修复后做完整启动测试

- [x] Task 8: 规划文档
  - 新增 `.trae/specs/multi-intent-orchestration/spec.md`
  - 新增 `.trae/specs/multi-intent-orchestration/tasks.md`
  - 新增 `.trae/specs/multi-intent-orchestration/checklist.md`

# Task Dependencies
- Task 1 独立
- Task 2 依赖 Task 1（读 agent_config）
- Task 3 依赖 Task 1（读 intent_config / model_config prompts）
- Task 4 依赖 Task 2 + Task 3（用 AgentFactory + GuardExecutor + IntentResult）
- Task 5 依赖 Task 2 + 3 + 4
- Task 6 依赖 Task 5
- Task 7 依赖 Task 6
- Task 8 依赖 Task 7
