# 会话停止接口设计方案

## 摘要

新增 `POST /chat/stop` 接口，前端调用后触发后端取消正在进行的会话主流程。机制：chat 路由用 `asyncio.Task` 包裹 SSE 生成器并注册到 `app.state` 的会话任务表；停止接口 `task.cancel()` 触发 `CancelledError`；`generate_response` 新增 `try/finally`，在 finally 中保证持久化（success=False 落库）+ langfuse trace flush + trace_id 存 Redis，最后给前端 yield `user_abort` 事件。

## 现状分析（已探索确认）

### 关键约束（探索报告结论）
1. **无取消机制**：当前 `StreamingResponse(stream())` 直接消费生成器，外部拿不到 Task 句柄；无 is_disconnected 检查。
2. **无 try/finally**：[chat_service.py:412-477](file:///workspace/app/services/chat_service.py#L412-L477) `generate_response` 纯顺序执行，中断会跳过 `_persist_conversation_history`（L459）和 `_finalize_trace`（L476）。仅 `with root_span_ctx` 靠 contextmanager 自动 end span。
3. **无会话任务注册表**：`app.state` 无 `dict[session_id, Task]`，需新建。
4. **OrchestratorService 单例污染**：`_last_orchestrator`/`_last_agent_ids`/`_last_success` 是实例属性，并发会话互相覆盖。中断时读这些属性不可信——多 agent 路径中断在 `orchestrator.run()` 中途时 `last_agent_ids=[]`、`last_success=True`（重置初值）。

### 现有 SSE 事件 type 清单（停止接口需新增 `user_abort`）
- chat_service: `session_ready`/`recommended_questions`/`trace_ready`/`files_generated`/`CUSTOM_COMPONENT`
- orchestrator run(): `orchestration_start`(direct)/`error`/`summary`/`query_rewritten`/`intent_step`/`intents_recognized`
- 子编排器: `orchestration_start`/`task_start`/`task_end`/`summary`/`pipeline_intercept`/`react_step`/`react_act`/`react_observe`/`react_final`/`react_timeout`
- agentscope 原生 AgentEvent 透传

### 需接入的资源（main.py lifespan 挂载）
- `app.state.orchestrator_service`（单例）
- `app.state.session_service`（持久化）
- `app.state.langfuse_service`（flush trace）
- `app.state.redis_client`（存 trace_id）

## 设计决策

1. **取消机制：Task.cancel() + finally**。chat 路由把 `stream()` 包成 `asyncio.Task` 注册到 `app.state.chat_tasks: dict[session_id, asyncio.Task]`；停止接口 `task.cancel()` 触发 `CancelledError`。即时中断，无需在 run() 各 await 点插轮询。
2. **中断也落库**：finally 中调用 `_persist_conversation_history`，`success` 强制写 False，`agent_ids` 用已收到事件中提取的（可能为空），`final_output` 用已收到的 summary/fallback 片段（可能为空字符串）。保证每轮都有 assistant 记录。
3. **URL：POST /chat/stop**，请求体 `{session_id}`。
4. **user_abort 事件**：在 finally 中 yield `{"type":"user_abort","session_id":...,"message":"用户已停止"}`，让前端 SSE 流读到后停止渲染。注意 finally 中 yield 在 CancelledError 后仍可执行（Python 生成器捕获 CancelledError 后可继续 yield）。
5. **trace 收尾**：finally 中调用 `_finalize_trace` flush langfuse，trace_id 存 Redis。
6. **不解决单例污染**：单例 `last_*` 并发污染是既有架构问题，本方案不在此修。中断持久化时不信任 `last_success`（强制 False），`last_agent_ids` 直接用（可能空，可接受）。
7. **StreamingResponse 与 Task 的关系**：FastAPI/Starlette 的 StreamingResponse 本身会驱动生成器。本方案不把 stream() 包成后台 Task，而是：保持 `StreamingResponse(stream())`，在 stream() 内部用 try/finally；停止接口通过注册的"取消句柄"中断生成器。**实现方式**：注册的不是 Task，而是 `asyncio.Event` 取消标志 + 生成器内轮询——但这与用户选的 Task.cancel() 冲突。
   - **修正决策**：采用"注册生成器引用 + task.cancel()"的混合方案。chat 路由仍返回 `StreamingResponse(stream())`，但额外启动一个**守护 Task** 包裹 stream() 的消费——不可行，StreamingResponse 必须直接消费生成器。
   - **最终方案**：保持 `StreamingResponse(stream())`。`stream()` 内部 `generate_response` 加 try/finally。**取消信号通过 `asyncio.Event` 传入 generate_response**，generate_response 在关键 yield 前检查 event.is_set()，是则主动 raise CancelledError 进入 finally。停止接口 `set()` 该 event。注册表存 `{session_id: asyncio.Event}` 而非 Task。
   - **理由**：StreamingResponse 驱动模型决定了无法外部 task.cancel()，只能用协作式取消标志。用户选的"Task.cancel()+finally"精神是"即时中断 + finally 保证清理"，用 Event 触发主动 raise + finally 可达成同等效果，且与 Starlette 流式架构兼容。

> **决策修订说明**：用户原选 Task.cancel()，但探索发现 StreamingResponse 阻止了外部 Task 取消。最终采用**协作式 Event 取消 + 主动 raise CancelledError + finally 清理**，实现用户期望的"即时中断 + finally 保证资源释放和持久化"。注册表为 `dict[session_id, asyncio.Event]`。

## 具体改动（3 个文件）

### 一、app/main.py — app.state 新增会话任务注册表

lifespan 中新增：
```python
app.state.chat_tasks: dict[str, asyncio.Event] = {}  # session_id → 取消标志
```
shutdown 时清空。

### 二、app/routes/chat.py — 注册取消标志 + 新增 /chat/stop 接口

#### 2.1 /chat 路由：创建并注册 Event，stream() 退出时清理

[chat.py:29-53](file:///workspace/app/routes/chat.py#L29-L53) 改造：
```python
# 创建取消标志并注册
cancel_event = asyncio.Event()
request.app.state.chat_tasks[session_id] = cancel_event

async def stream():
    try:
        yield _event({"type": "session_ready", "session_id": session_id})
        async for event_str in generate_response(
            ...,
            cancel_event=cancel_event,  # 新增：传入取消标志
        ):
            yield event_str
    finally:
        # 流结束（正常或异常）后清理注册表
        request.app.state.chat_tasks.pop(session_id, None)

return StreamingResponse(stream(), media_type="text/event-stream")
```

#### 2.2 新增 POST /chat/stop 接口

```python
class StopRequest(BaseModel):
    session_id: str

@router.post("/chat/stop")
async def stop_chat(
    request: Request,
    body: StopRequest,
    user: dict = Depends(current_user),
):
    user_id = user.get("user_id")
    session_id = body.session_id
    cancel_event = request.app.state.chat_tasks.get(session_id)
    if cancel_event is None:
        return {"ok": False, "msg": "会话不在运行中或已结束"}
    # 触发取消：generate_response 检测到后主动 raise CancelledError
    cancel_event.set()
    return {"ok": True, "msg": "已发送停止信号"}
```

> 停止接口立即返回，实际中断由 generate_response 的 finally 异步完成，user_abort 事件通过原 SSE 流推送。

### 三、app/services/chat_service.py — generate_response 接收 cancel_event + try/finally + user_abort

#### 3.1 签名新增 cancel_event 参数

```python
async def generate_response(
    ...,
    skills: List[str] = None,
    cancel_event: Optional[asyncio.Event] = None,  # 新增
) -> AsyncGenerator[str, None]:
```

#### 3.2 主体包裹 try/finally

```python
# 构造一个检查取消的辅助
def _cancelled():
    return cancel_event is not None and cancel_event.is_set()

aborted = False
try:
    with root_span_ctx as root_obs:
        async for event_str in orchestrator_service.run(...):
            if _cancelled():
                aborted = True
                raise asyncio.CancelledError()
            # ...解析事件、收集 final_output_parts / final_fallback_parts...
        # ...final_output 计算...
        if _cancelled():
            aborted = True
            raise asyncio.CancelledError()
        # ...recommended_questions / 持久化 / files_generated / span 更新...
except asyncio.CancelledError:
    aborted = True
    # 吞掉 CancelledError，进入 finally 做清理（不重新 raise，让流优雅结束）
finally:
    # 1. 中断时也落库：success 强制 False
    if aborted:
        final_output = final_output or "\n".join(p for p in final_fallback_parts if p).strip()
        await _persist_conversation_history(
            orchestrator_service, session_service, session_id, user_id,
            messages, final_output,
        )
        # 强制把刚落库的 assistant 消息 success 置 False（_persist 内部用 last_success，
        # 中断时不可信，这里补一道 UPDATE）
        await _mark_last_message_failed(session_service, session_id, user_id)
    # 2. langfuse trace flush + trace_id 存 Redis
    async for ev in _finalize_trace(...):
        yield ev
    # 3. user_abort 事件（中断时）
    if aborted:
        yield _event({"type": "user_abort", "session_id": session_id, "message": "用户已停止"})
```

#### 3.3 新增 _mark_last_message_failed 辅助

中断时 `_persist_conversation_history` 内部读 `orchestrator_service.last_success`（单例污染，可能为 True）。为保证落库 success 正确，新增辅助直接 UPDATE 该 session 最新 assistant 消息 success=0：

```python
async def _mark_last_message_failed(session_service, session_id, user_id):
    """中断时强制把该会话最新 assistant 消息标记为失败。"""
    # 通过 session_service / session_dao 执行 UPDATE
    ...
```
> 具体实现依赖 session_dao 是否有按 session_id 更新消息的接口，探索未深入 dao 层；实现时若 dao 无现成接口则新增一个 `mark_last_assistant_failed(session_id, user_id)` 方法。

## 不改动的部分

- `orchestrator_service.run()` 签名与内部逻辑（取消检查点放在 generate_response 的 `async for` 循环顶部，run() 内部不加检查——LLM 流式 chunk 之间会回到 generate_response 的循环，可在此检测取消）
- 子编排器（parallel/pipeline/react）内部逻辑
- `_persist_conversation_history` 主逻辑（仅中断时补一道 UPDATE 强制失败）
- `intent_recognition`/`intent_orchestration` 等业务逻辑
- 认证流程

## 假设与决策

1. **协作式取消（Event）替代 Task.cancel()**：StreamingResponse 驱动模型决定了无法外部 task.cancel()，用 Event 触发 generate_response 主动 raise CancelledError 达成同等效果。取消粒度为"事件之间"（LLM chunk 之间），非 chunk 内部中断。
2. **中断也落库 success=False**：保证每轮都有 assistant 记录，便于审计。final_output 可能为空字符串（中断在 summary 之前）。
3. **不信任单例 last_success**：补 `_mark_last_message_failed` 强制 UPDATE，绕开单例污染。
4. **user_abort 在 finally 中 yield**：Python 生成器捕获 CancelledError 后可继续 yield，前端 SSE 流能读到。
5. **停止接口立即返回**：不等待实际中断完成，中断异步进行。前端通过原 SSE 流收到 user_abort 后停止渲染。
6. **注册表 key 为 session_id**：session_id 在 session_ready 事件中已回传前端，停止请求携带同一 session_id。
7. **未并发安全地处理重复停止**：第二次 stop 调用时 event 已 set，幂等返回 ok。

## 验证步骤

1. **AST 校验**：main.py / chat.py / chat_service.py
2. **正常流不受影响**：不调用 stop 时，generate_response 正常跑完，finally 中 aborted=False，不落失败记录，不 yield user_abort
3. **中断流**：调用 /chat/stop 后，generate_response 在下一个事件循环顶部检测到 cancel_event，raise CancelledError → finally 落库 success=False → flush trace → yield user_abort
4. **注册表清理**：流结束（正常/异常/中断）后 chat_tasks.pop(session_id) 执行
5. **幂等停止**：对已结束会话调 stop 返回 ok=False
6. **并发会话**：两个 session_id 各自独立 Event，互不干扰
7. **git commit & push** 到 `origin/trae/agent-5CYjia`
