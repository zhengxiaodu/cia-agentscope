# 单智能体直接问答功能开发计划

## 总结

当前 `/chat` 接口始终走完整的"查询改写 → 意图识别 → 编排器选择 → 编排执行"流程。本需求允许前端在调用 `/chat` 时可选传入 `agent_id`，若传入则跳过上述流程，直接调用该 agent 及其配置的 skills 进行单 agent 回答；不传则保持原有完整流程；若 `agent_id` 不存在则返回错误。

## 当前状态分析

### 调用链路

```
routes/chat.py#chat()
  → chat_service.py#generate_response()
    → orchestrator_service.py#OrchestratorService.run()
      ① QueryRewriter.rewrite()      — 查询改写
      ② IntentRecognizer.recognize() — 意图识别
      ③ _select_orchestrator()       — 选择编排器
      ④ orchestrator.run()           — 编排执行（parallel/pipeline/react）
```

### 关键数据结构

- **`ChatRequest`** (`app/models/chat.py`): 当前只有 `messages` 和 `session_id` 两个字段
- **`OrchestratorService.run()`** (`app/services/orchestrator_service.py`): 参数为 `messages`, `session_id`, `user_id`, `session_service`
- **`AgentRegistry`** (`app/agents/registry.py`): 持有所有 agent 定义，提供 `get_definition(agent_id)` 检查 agent 是否存在，`create_agent()` 创建 agent 实例
- **`AgentFactory`** (`app/agents/factory.py`): 封装 registry 提供 `create_for_agent()` 方法
- **`BaseOrchestrator._run_single_agent()`** (`app/orchestrator/base.py`): 已实现单个 agent 的执行逻辑，接收 `Intent` 对象

## 改动方案

需要修改 3 个文件：

### 1. `app/models/chat.py` — 请求模型增加字段

- 在 `ChatRequest` 中增加可选字段 `agent_id: Optional[str] = None`
- 位置：与现有 `session_id` 并列

### 2. `app/routes/chat.py` — 透传 agent_id

- 在 `ChatRequest` 解包后，将 `body.agent_id` 传入 `generate_response()`

### 3. `app/services/chat_service.py` — 增加参数透传

- `generate_response()` 签名增加 `agent_id: Optional[str] = None` 参数
- 调用 `orchestrator_service.run()` 时透传 `agent_id=agent_id`

### 4. `app/services/orchestrator_service.py` — 核心改动

**`run()` 方法增加 `agent_id` 参数**，在方法开头（现有 `user_input` 检查之后）插入快速路径：

```
如果 agent_id 不为空:
  1. 通过 self.registry.get_definition(agent_id) 检查 agent 是否存在
  2. 如果不存在 → yield error 事件并 return
  3. 如果存在 → 跳过 ①改写 ②识别 ③编排器选择 ④编排器.run()
     直接:
     a. yield orchestration_start 事件 (mode="direct")
     b. 构造单意图 Intent(id=f"direct_{agent_id}", query=user_input, agent=agent_id)
     c. 加载已有 AgentState（如果 session 存在）
     d. 调用 self._run_single_agent(intent, session_id, agent_state)
     e. yield 回放 agent 事件
     f. yield summary 事件
     g. 持久化 AgentState
     h. 保存 self._last_orchestrator = 临时编排器包装
     i. return (不再走后续流程)
```

**关键实现细节**：

- `_run_single_agent` 是 `BaseOrchestrator` 的方法，所以在 `OrchestratorService` 中不能直接调用。解决方案：
  - 方案 A：将 `_run_single_agent` 提升为 `OrchestratorService` 的静态/类方法，或提取到模块级函数
  - 方案 B：新建一个临时的轻量编排器对象来调用 `_run_single_agent`
  
  推荐方案 A：在 `BaseOrchestrator` 中将 `_run_single_agent` 改为不需要 `self`（只依赖 `self.agent_factory`），然后在 `OrchestratorService` 中直接复用 `self.agent_factory` 调用相似逻辑。

  更简洁的方案：直接在 `OrchestratorService` 中内联 `_run_single_agent` 逻辑（约 30 行），因为此时不需要复用编排器的其他方法。

  实际上阅读 `_run_single_agent` 的代码后发现它只依赖 `self.agent_factory`，而 `OrchestratorService` 已经有 `self.agent_factory`。所以可以直接在 `OrchestratorService` 中实现单 agent 执行的逻辑，复用 `agent_factory.create_for_agent()` 和 Agent 的 `reply_stream`。

- **AgentState 加载**：复用现有逻辑，通过 `session_service.load_agent_state(session_id, agent_id)` 加载
- **事件 SSE 格式**：遵循现有事件格式，产生 `orchestration_start`, agent 的 reply 事件, `summary`, `trace_ready`

## 假设与决策

| 决策 | 选择 |
|------|------|
| agent_id 校验方式 | 通过 `registry.get_definition(agent_id)` 检查是否存在 |
| 单 agent 执行方式 | 在 `OrchestratorService.run()` 中直接调用 `agent_factory.create_for_agent()` + `agent.reply_stream()`，不走现有编排器类 |
| 事件流 | 直接透传 agent 的 reply_stream 事件，跳过 intent_recognized / query_rewritten 等中间事件 |
| 错误处理 | agent_id 不存在 → yield `{"type": "error", "message": "agent_id xxx 不存在"}` 并 return |

## 验证步骤

1. 不传 `agent_id` 调用 `/chat` → 正常走完整编排流程
2. 传入有效的 `agent_id`（如 `search_agent`）调用 `/chat` → 直接调用该 agent，返回其回答
3. 传入无效的 `agent_id` 调用 `/chat` → 返回错误信息