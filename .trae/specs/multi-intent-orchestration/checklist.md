# Checklist

## 配置层
- [x] `config/agent_config.yml` 定义 3 个智能体，每个含 id/name/skills/system_prompt
- [x] `config/intent_config.yml` 定义意图 + 意图→智能体映射 + 守护意图 + 默认编排策略
- [x] `config/model_config.yml` 新增 `intent_recognizer` 模型段（低温度保证 JSON 稳定）
- [x] `config/model_config.yml` 新增 `prompts` 段（rewrite / intent_recognition / react_think）
- [x] `app/config.py` 新增 `AGENT_CONFIG_PATH` / `INTENT_CONFIG_PATH` 常量

## 智能体层 `app/agents/`
- [x] `AgentDefinition` 数据类含 id/name/skills/system_prompt
- [x] `AgentRegistry` 按 skill 子集为每个智能体组装独立 Toolkit
- [x] `AgentRegistry.create_agent` 每次创建新模型实例（流式不可复用）
- [x] `AgentFactory.create_for_agent` 按 agent_id 路由
- [x] `AgentFactory.create_fallback` 降级到 general_agent

## 意图识别层 `app/intent/`
- [x] `Intent` / `IntentResult` / `IntentConfig` / `GuardIntentConfig` / `GuardResult` 数据模型
- [x] `llm_client` 非流式 LLM 调用（AsyncOpenAI）
- [x] `extract_json` 处理 markdown 代码块 + 花括号匹配兜底
- [x] `QueryRewriter` 无上下文时跳过 LLM 调用
- [x] `IntentRecognizer` 未知意图 id 降级为 general_chat
- [x] `IntentRecognizer` 空意图列表兜底为 general_chat
- [x] `IntentRecognizer` 识别失败降级（_fallback）
- [x] `get_orchestration_mode` relation 直接决定模式（单意图 related_dynamic 也走 react）

## 编排层 `app/orchestrator/`
- [x] `BaseOrchestrator._run_single_agent` 收集所有 SSE 事件 + 提取文本输出
- [x] `ParallelOrchestrator` 用 `asyncio.gather` 并行 + 超时控制 + 汇总
- [x] `PipelineOrchestrator` 串行 + 每步守护检查 + 失败拦截终止
- [x] `ReActOrchestrator` Thought→Act→Observe 循环 + MAX_STEPS 兜底 + is_final 提前终止
- [x] `GuardExecutor` 高风险拦截 + 中风险警告 + 未命中放行
- [x] 三编排器均先执行守护意图前置检查

## 服务与路由层
- [x] `OrchestratorService.create` 工厂方法串联所有组件
- [x] `OrchestratorService.run` 主流程：改写→识别→选择编排器→执行
- [x] `chat_service.generate_response` 转调 orchestrator_service，保留 session + Langfuse + CUSTOM_COMPONENT
- [x] `chat.py` 路由参数改为 orchestrator_service
- [x] `health.py` `/skills` → `/agents`
- [x] `main.py` lifespan 用 OrchestratorService.create() 初始化
- [x] `SessionDAO` 新增独立消息历史存储（`session_msgs:{id}`）
- [x] `SessionService` 新增 load_messages / append_messages

## 兼容性
- [x] 保留 `create_model_from_config` / `load_model_config` 旧函数
- [x] 保留 `load_skills` 向后兼容（标记废弃）
- [x] 3 个 skill 目录与内容不变
- [x] SSE 事件单意图场景退化为原有行为
- [x] 意图识别失败降级保证可用性

## 验证
- [x] 全部新增/修改文件 AST 语法检查通过
- [x] 三个配置文件解析正确
- [x] 意图识别逻辑测试通过（模式选择/降级/兜底/JSON 提取）
- [x] 守护执行器逻辑测试通过（拦截/警告/放行）
- [ ] 完整启动测试（受阻于环境 sqlalchemy 版本问题，需 `pip install --upgrade sqlalchemy`）

## 文档
- [x] `.trae/specs/multi-intent-orchestration/spec.md`
- [x] `.trae/specs/multi-intent-orchestration/tasks.md`
- [x] `.trae/specs/multi-intent-orchestration/checklist.md`
