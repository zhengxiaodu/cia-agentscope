# 编排器事件流实时透传 Spec

## Why
当前三种编排器（pipeline / parallel / react）在执行智能体时，把 `agent.reply_stream` 产生的事件收集到 `TaskResult.events` 列表，等智能体执行完毕后才回放。这导致流式输出卡顿：前端无法看到每个意图实时的执行进度（如逐字生成、工具调用），必须等整个智能体跑完才一次性收到所有事件。用户期望事件实时透传，提升流式体验。

## What Changes
- **重构 `BaseOrchestrator._run_single_agent`**：从 `async def -> TaskResult` 改为 `async def -> AsyncGenerator[Union[str, TaskResult], None]`，实时 yield SSE 事件字符串，最后 yield TaskResult（events 字段保留为空，因已实时透传）
- **Pipeline 适配**：把 `asyncio.wait_for` 超时改为 `asyncio.timeout` 上下文管理器（Python 3.11+，支持 async generator）；`async for` 透传事件 + 捕获最终 TaskResult
- **Parallel 适配**：引入 `asyncio.Queue`，每个 agent task 把事件推入队列，主循环从队列取并 yield；超时按 agent 独立控制
- **ReAct 适配**：`_execute_action` 改为 async generator yield 事件 + 最后 yield output；`run()` 中 `async for` 透传事件并提取 observation
- **保留 `TaskResult.events` 字段**：向后兼容（保留为空列表），避免破坏 `last_agent_states` 等依赖
- **BREAKING**：`_run_single_agent` 签名变更，返回类型从 `TaskResult` 改为 async generator；所有内部调用方需适配

## Impact
- Affected specs: multi-intent-orchestration（编排器核心能力）
- Affected code:
  - [app/orchestrator/base.py](file:///workspace/app/orchestrator/base.py) — `_run_single_agent` 重构
  - [app/orchestrator/pipeline.py](file:///workspace/app/orchestrator/pipeline.py) — 串行透传 + 超时改造
  - [app/orchestrator/parallel.py](file:///workspace/app/orchestrator/parallel.py) — Queue 模式
  - [app/orchestrator/react.py](file:///workspace/app/orchestrator/react.py) — `_execute_action` 改 generator
- 不影响：`orchestrator_service.py`（只 `async for event in orchestrator.run(...)` 透传，不感知内部变化）、chat_service.py、前端 SSE 协议（事件格式不变）

## ADDED Requirements

### Requirement: 编排器事件实时透传
系统 SHALL 在智能体执行过程中，实时把 `agent.reply_stream` 产生的每个事件透传给上层（orchestrator_service → chat_service → SSE 响应），而非等到智能体执行完毕后批量回放。

#### Scenario: Pipeline 单步实时透传
- **WHEN** PipelineOrchestrator 执行第 i 个意图，agent 产生 `ReplyStartEvent` 后开始逐字生成文本块
- **THEN** 每个文本块事件在产生时立即被 yield 给上层，前端实时看到逐字输出
- **AND** 该步骤执行完毕后，`task_end` 事件携带 success/output，进入下一步

#### Scenario: Parallel 多 agent 事件交错
- **WHEN** ParallelOrchestrator 并行执行 3 个意图（agent A/B/C），三者同时产生事件
- **THEN** 事件按产生顺序交错 yield 给上层（通过 asyncio.Queue 汇聚），前端实时看到三个 agent 的进度
- **AND** 某个 agent 先完成时立即发出该 agent 的 `task_end`，不等其他 agent
- **AND** 所有 agent 完成后发出 `summary`

#### Scenario: ReAct 单步实时透传
- **WHEN** ReActOrchestrator 执行某步的 `call_agent` 动作，agent 产生事件
- **THEN** 事件实时 yield 给上层，前端看到该步推理-执行的实时进度
- **AND** 该 agent 执行完毕后，output 作为 observation 进入 scratch，继续下一步 think

#### Scenario: 智能体执行异常不阻断事件流
- **WHEN** 智能体执行中抛异常（如 LLM 调用失败）
- **THEN** 已产生的事件已实时透传给前端，异常被捕获后 yield 一个 success=False 的 TaskResult
- **AND** 编排器按原逻辑处理失败（pipeline 终止 / parallel 标记该任务失败 / react 返回失败 observation）

### Requirement: 超时控制兼容 async generator
系统 SHALL 对改为 async generator 的智能体执行流保留超时控制能力。

#### Scenario: Pipeline 单步超时
- **WHEN** PipelineOrchestrator 某步骤执行超过 `step_timeout`（默认 60s）
- **THEN** 该步骤被取消，yield 一个 success=False 的 TaskResult（output="步骤执行超时"）
- **AND** 流水线按原逻辑终止后续步骤

#### Scenario: Parallel 单 agent 超时
- **WHEN** ParallelOrchestrator 某 agent 执行超过 `timeout`（默认 60s）
- **THEN** 该 agent 被取消，yield 该 agent 的 `task_end`（success=False）
- **AND** 其他 agent 不受影响继续执行

## MODIFIED Requirements

### Requirement: BaseOrchestrator._run_single_agent
原签名：`async def _run_single_agent(intent, ...) -> TaskResult`，内部收集事件到 `result.events`。

改为：`async def _run_single_agent(intent, ...) -> AsyncGenerator[Union[str, TaskResult], None]`：
- 创建 agent、构建 user_msg 逻辑不变
- `async for event in agent.reply_stream(user_msg)` 循环中，每个 AgentEvent 实时 `yield f"data: {event.model_dump_json()}\n\n"`
- 仍用 `apply.append_event(event)` 收集文本块用于提取 output
- 循环结束后提取 output、捕获 final_state、设置 success
- 最后 `yield result`（TaskResult，events 字段为空列表）
- 异常分支：捕获后设置 success=False，仍 `yield result`
- 仍由调用方负责超时包装

### Requirement: TaskResult.events 字段
保留字段以向后兼容，但默认为空列表（不再用于回放）。`last_agent_states` 等依赖 `final_state` 的逻辑不受影响。
