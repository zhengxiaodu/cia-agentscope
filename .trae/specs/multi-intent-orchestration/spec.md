# 多智能体 + 多意图编排 Spec

## Why

当前系统只有一个智能体（`Agent` 单实例），所有用户请求都由同一个 system_prompt + 全量 skill 处理，无法按意图路由到专用智能体，也无法处理单条输入中包含的多个意图。

参考《智能体多意图场景设计文档》，需要实现：
- **多智能体**：按 skill 能力划分多个智能体，各司其职
- **意图识别**：把用户输入改写并联系上下文，识别显式意图 + 守护意图
- **工厂路由**：识别出的意图通过工厂调用绑定的智能体和技能
- **多意图编排**：根据意图间关系（无关联/有关联固定/有关联动态）选择并行调度、写死流水线、ReAct 动态编排三种策略

## What Changes

### 新增配置
- **`config/agent_config.yml`** —— 3 个智能体定义（search_agent / chart_agent / general_agent），含 system_prompt 与绑定的 skill
- **`config/intent_config.yml`** —— 意图定义、意图→智能体映射、守护意图、默认编排策略、编排器参数
- **`config/model_config.yml`（扩展）** —— 新增 `intent_recognizer` 模型配置 + `prompts` 段（rewrite / intent_recognition / react_think）

### 新增代码模块
- **`app/agents/`** —— 智能体层
  - `base.py`：`AgentDefinition` 数据类
  - `registry.py`：`AgentRegistry` 加载配置、按 skill 子集组装独立 Toolkit、创建 Agent 实例
  - `factory.py`：`AgentFactory` 意图→智能体门面（含兜底降级）
- **`app/intent/`** —— 意图识别层
  - `models.py`：`Intent` / `IntentResult` / `IntentConfig` / `GuardIntentConfig` / `GuardResult`
  - `llm_client.py`：非流式 LLM 调用 + JSON 提取（`AsyncOpenAI` + `extract_json`）
  - `rewriter.py`：`QueryRewriter` 查询改写（联系上下文补全省略/指代）
  - `recognizer.py`：`IntentRecognizer` LLM 单次意图识别（输出结构化 JSON）
- **`app/orchestrator/`** —— 编排层
  - `base.py`：`TaskResult` + `BaseOrchestrator`（共享单智能体执行 + SSE 事件）
  - `guard.py`：`GuardExecutor` 守护意图执行器（合规/风控/确认，可拦截）
  - `parallel.py`：`ParallelOrchestrator` 并行调度（`asyncio.gather`）
  - `pipeline.py`：`PipelineOrchestrator` 写死流水线（串行 + 守护拦截）
  - `react.py`：`ReActOrchestrator` ReAct 循环（Thought→Act→Observe）
- **`app/services/orchestrator_service.py`** —— 编排服务：串起改写→识别→编排，对外暴露 `run()`

### 修改代码
- **`app/config.py`** —— 新增 `AGENT_CONFIG_PATH` / `INTENT_CONFIG_PATH` 常量
- **`app/services/chat_service.py`** —— 重构为适配层：`generate_response` 转调 `OrchestratorService.run()`，保留 session 历史持久化 + Langfuse + CUSTOM_COMPONENT 检测
- **`app/dao/session_dao.py`** —— 新增 `load_messages` / `append_messages`（独立于 AgentState 的纯消息历史存储）
- **`app/services/session_service.py`** —— 新增 `load_messages` / `append_messages` 转发
- **`app/routes/chat.py`** —— `generate_response` 参数从 `toolkit`/`model_config` 改为 `orchestrator_service`
- **`app/routes/health.py`** —— `/skills` 改为 `/agents`，从 `orchestrator_service.registry` 取智能体列表
- **`app/main.py`** —— lifespan 中用 `OrchestratorService.create()` 替代 `load_skills()`，存入 `app.state.orchestrator_service`

## Impact

- Affected specs: chat（流式处理流程重构）、multi-turn-session（会话历史改用独立消息列表存储）
- Affected code: 见上 What Changes
- 向后兼容：保留 `create_model_from_config` / `load_model_config` / `load_skills` 旧函数签名；SSE 事件在单意图场景退化为原有行为；skill 目录与内容不变

## ADDED Requirements

### Requirement: 多智能体按 skill 能力划分
系统 SHALL 提供 3 个智能体，各自绑定不同 skill 子集，实现职责隔离。

- `search_agent` 绑定 `bocha_search`，处理实时信息检索
- `chart_agent` 绑定 `chart_renderer` + `card_interaction`，处理数据可视化
- `general_agent` 无 skill，处理通用问答与兜底

#### Scenario: 新增智能体
- **WHEN** 在 `agent_config.yml` 添加一条 agent 记录，并在 `intent_config.yml` 绑定意图
- **THEN** 重启后新智能体自动注册，无需改代码

### Requirement: 查询改写联系上下文
系统 SHALL 在意图识别前，结合历史对话上下文对用户输入进行改写，补全省略、指代。

#### Scenario: 有上下文的省略输入
- **WHEN** 用户在前文讨论"招商银行"后输入"它今天涨了多少"
- **THEN** 改写器输出"招商银行今天涨了多少"，再进入意图识别

#### Scenario: 无上下文
- **WHEN** 无历史对话
- **THEN** 原样返回用户输入，不调用改写 LLM

### Requirement: LLM 单次意图识别
系统 SHALL 通过一次 LLM 调用，输出结构化 JSON 识别多意图、意图间关系、守护意图。

#### Scenario: 单意图
- **WHEN** 用户输入"今天有什么新闻"
- **THEN** 识别为 1 个 `search_info` 意图，relation=independent，走并行

#### Scenario: 多意图无关联
- **WHEN** 用户输入"查一下新闻，再画个图"
- **THEN** 识别为 2 个意图（search_info + render_chart），relation=independent，走并行调度

#### Scenario: 多意图有关联固定
- **WHEN** 用户输入"查一下数据然后画成柱状图"
- **THEN** relation=related_fixed，走写死流水线（先查后画，串行）

#### Scenario: 复杂动态
- **WHEN** 用户输入"分析降息对持仓的影响并给建议"
- **THEN** relation=related_dynamic，走 ReAct 动态编排

#### Scenario: 识别失败降级
- **WHEN** LLM 输出无法解析或调用异常
- **THEN** 降级为单 `general_chat` 意图，保证可用性

### Requirement: 三种编排模式
系统 SHALL 根据意图间关系选择编排策略。

#### Scenario: 并行调度（无关联）
- **WHEN** relation=independent 且多意图
- **THEN** `asyncio.gather` 并行执行各智能体，各自返回结果后汇总

#### Scenario: 写死流水线（有关联固定）
- **WHEN** relation=related_fixed
- **THEN** 按固定顺序串行执行，每步输出作为下一步上下文；任一步失败则终止

#### Scenario: ReAct 动态编排（有关联动态）
- **WHEN** relation=related_dynamic
- **THEN** 进入 Thought→Act→Observe 循环，LLM 动态决策下一步，最多 `react_max_steps` 步，可提前终止

### Requirement: 守护意图不可省略
系统 SHALL 在编排关键节点执行守护意图检查，合规/风控类守护失败时拦截流程。

#### Scenario: 高风险拦截
- **WHEN** 用户输入命中 `confirm_action`（含买入/卖出/转账等）高风险关键词
- **THEN** 发送 `guard_intercept` 事件，终止流程并提示用户

#### Scenario: 中风险警告
- **WHEN** 命中 `risk_warning`（含建议/投资等）中风险关键词
- **THEN** 附加风险提示但继续执行（`guard_warning` 事件）

#### Scenario: 未命中
- **WHEN** 未命中任何守护关键词
- **THEN** 放行，无额外事件

### Requirement: SSE 事件协议扩展
系统 SHALL 在现有事件基础上新增编排相关事件，前端可展示意图识别与编排过程。

新增事件类型：`query_rewritten` / `intents_recognized` / `orchestration_start` / `task_start` / `task_end` / `summary` / `guard_checking` / `guard_intercept` / `guard_warning` / `react_step` / `react_act` / `react_observe` / `react_final` / `pipeline_intercept`

#### Scenario: 单意图退化兼容
- **WHEN** 单意图场景
- **THEN** 事件序列简化为 `orchestration_start` → `task_start` → (agent 事件) → `task_end` → `summary`，与原有行为平滑过渡

## MODIFIED Requirements

### Requirement: POST /chat（修改）
- `ChatRequest` 模型不变
- 内部流程改为：加载会话历史 → 改写 → 意图识别 → 编排执行 → 持久化
- `generate_response` 参数从 `(toolkit, model_config, ...)` 改为 `(orchestrator_service, ...)`
- 会话历史改用独立消息列表存储（`session_msgs:{id}`），不再依赖单 AgentState

### Requirement: 会话历史持久化（修改）
- 新增 `SessionDAO.load_messages` / `append_messages`，用独立 Redis key 存纯消息列表
- 多智能体场景下，每轮对话保存 user 输入 + summary 输出
- 原 AgentState 持久化机制保留（向后兼容），但 `/chat` 主流程改用消息列表

## REMOVED Requirements
无

## 设计原则（来自参考文档第五章）

1. 能并行就并行：无关联任务用 `asyncio.gather`
2. 能写死就写死：固定流程用硬编码流水线保证确定性
3. 复杂场景引入 ReAct：路径不确定时让智能体自主规划
4. 复杂意图设计思维链：ReAct 预置推理模板避免跳步
5. 守护意图不可省略：作为硬性约束嵌入流程
