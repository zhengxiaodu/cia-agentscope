# 历史限三轮 + Langfuse 环节埋点 + 推荐问题前置

## 一、需求摘要

三处改动（用户已澄清真实意图）：

1. **历史限三轮**：大模型上下文容量仅 20K token，需统一限制历史为最近 3 轮（6 条消息）。涉及三处：
   - `chat_service.py` 加载 `history_messages` 后截断为最后 6 条
   - `orchestrator_service.py` 加载 `state_dict` 后截断 `context` 为最后 6 条
   - `recognizer.py` / `rewriter.py` 的 `history[-6:]` 保持不动（已是 3 轮）
2. **Langfuse 环节埋点**：当前只记录整体对话耗时（一个根 span）。改为为每个环节起**子 span**：问题改写、意图识别、每一次智能体调用、推荐问题生成。
3. **推荐问题前置**：当前推荐问题在「持久化历史 → 文件检测」之后才生成。改为编排流结束（yield summary）后**立即生成**并 yield，让前端尽快拿到；持久化/文件检测/trace 照旧在后。

## 二、现状分析（基于 Phase 1 探索，已核实）

### 2.1 历史加载链路（全量，需加截断）

- [app/services/chat_service.py](file:///workspace/app/services/chat_service.py) L173-183：`session_service.load_messages(session_id)` 从 messages 表全量加载（dao 层 `SELECT ... ORDER BY id ASC` 无 LIMIT，见 [mysql_session_dao.py:145-148](file:///workspace/app/dao/mysql_session_dao.py)），拼成 `history_messages` 后与当前 `messages` 合并成 `full_messages` 传给 `orchestrator_service.run()`。
- [app/services/orchestrator_service.py](file:///workspace/app/services/orchestrator_service.py) L494-498 / L621-625：`session_service.load_agent_state(session_id, agent_id)` 从 agent_states 表加载完整 `state` JSON（dao 层无截断，见 [mysql_session_dao.py:68-81](file:///workspace/app/dao/mysql_session_dao.py)）。`AgentState.context` 是完整对话历史列表（见 [mysql_session_dao.py:667-714 extract_messages_from_state](file:///workspace/app/dao/mysql_session_dao.py)，`context` 即 Msg_dict 列表）。
- [app/intent/recognizer.py:92](file:///workspace/app/intent/recognizer.py) `recent = history[-6:]`、[app/intent/rewriter.py:69](file:///workspace/app/intent/rewriter.py) `recent = history[-6:]`：已限 3 轮，**不改**。
- `_extract_history`（[orchestrator_service.py:215-229](file:///workspace/app/services/orchestrator_service.py)）返回全量 messages[:-1]，作为 `history` 传给 recognizer/rewriter（它们内部再 -6），**不改**。

→ 截断点应在「加载后、使用前」：chat_service 的 `history_messages`、orchestrator 的 `state_dict["context"]`。

### 2.2 Langfuse 现状（只有根 span，无环节埋点）

- [app/services/langfuse_service.py](file:///workspace/app/services/langfuse_service.py) `LangfuseService`：用 v3 SDK（`requirements.txt:19 langfuse>=3.5.0`），`start_observation` 调 `self._client.start_observation(name, as_type, input)` 返回 observation 对象，`end_observation` 调 `observation.update(output); observation.end()`。
- [app/services/chat_service.py:189-199](file:///workspace/app/services/chat_service.py) `generate_response` 启动时 `obs = langfuse_service.start_observation(name="chat-response", as_type="span", input={...})`，结束时 `langfuse_service.end_observation(obs, output={...})` + `flush()`（L360-373）。**全程一个根 span，无子 span**。
- 编排主流程在 `orchestrator_service.run()`（[orchestrator_service.py:422-659](file:///workspace/app/services/orchestrator_service.py)）内，包含改写（L577）、识别（L590）、编排执行（L634）、单 agent 直接问答（L523）。
- 推荐 question 生成在 [chat_service.py:347-354](file:///workspace/app/services/chat_service.py) `_generate_recommended_questions`。

→ 需在 LangfuseService 新增子 span 能力，并在各环节调用点包裹。

### 2.3 推荐问题时机现状

[chat_service.py](file:///workspace/app/services/chat_service.py) `generate_response` 当前顺序：
1. L213-240 编排流（yield 事件 + 收集 final_output）
2. L244-306 持久化历史
3. L308-328 文件检测 + yield files_generated
4. L330-356 **生成推荐问题 + yield recommended_questions**
5. L358-376 trace end + yield trace_ready

→ 推荐问题生成需移到第 1 步之后、第 2 步之前。

## 三、方案设计

### 3.1 修改 `app/services/langfuse_service.py`（新增子 span 能力）

新增方法 `start_span`（用 v3 的 `start_as_current_observation` context manager，自动 OTel 嵌套）：

```python
from contextlib import contextmanager

@contextmanager
def start_span(self, name: str, input: Any = None):
    """启动一个子 span（context manager，自动嵌套到当前活跃 observation 下）。

    用法：
        with langfuse_service.start_span("query-rewrite", input={...}) as span:
            ...
            if span:
                span.update(output={...})
    未启用 langfuse 时 yield None，不报错。
    """
    if not self._enabled or not self._client:
        yield None
        return
    try:
        with self._client.start_as_current_observation(
            as_type="span", name=name, input=input,
        ) as obs:
            yield obs
    except Exception as e:
        logger.warning("Langfuse start_span failed: %s", e)
        yield None
```

同时将根 observation 也改为 `start_as_current_observation`，使子 span 能自动嵌套到根 span 下。改 `generate_response` 中根 obs 的启动方式（见 3.2）。

> 注：v3 SDK 的 `start_as_current_observation` 依赖 OTel context propagation，子 span 自动成为当前活跃 span 的 child，无需手动传 parent。根 span 必须用同一机制启动并保持活跃（context manager 包裹整个函数体），子 span 才能嵌套。

### 3.2 修改 `app/services/chat_service.py`

#### (a) 历史截断

L173-183 加载 `history_messages` 后，截断为最后 6 条：

```python
history_messages = []
if session_service and session_id:
    try:
        saved = await session_service.load_messages(session_id)
        history_messages = [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in saved
            if m.get("content")
        ]
        # 限制历史为最近 3 轮（6 条消息），避免超出模型 20K token 上下文
        history_messages = history_messages[-6:]
    except Exception:
        logger.exception("[chat_service] 加载会话历史失败")
```

#### (b) 根 span 改用 start_as_current_observation + 包裹整个函数体

将 L189-199 的 `obs = langfuse_service.start_observation(...)` 改为用 `start_span` context manager 包裹后续主流程。由于 `generate_response` 是 async generator，不能用 `with` 直接包裹 `yield`（async generator 内 `with` 包裹 yield 是合法的，context manager 在 yield 期间保持活跃）。结构：

```python
with langfuse_service.start_span(
    "chat-response",
    input={"messages": full_messages, "session_id": session_id, "user_id": user_id},
) as root_obs:
    # ... 原有编排流、持久化、文件检测、推荐问题、trace 逻辑 ...
    # 子 span 在 orchestrator_service.run 内部启动（见 3.3）
    if root_obs:
        try:
            root_obs.update(output={"reply": final_output, "session_id": session_id, "user_id": user_id})
        except Exception:
            pass
# context manager 退出时自动 end root_obs
langfuse_service.flush()
trace_id = root_obs.trace_id if root_obs else None
```

> 注意：`root_obs` 在 with 块外仍可访问其 `trace_id`（observation 对象 end 后仍持 id）。

#### (c) 推荐问题生成前置 + 子 span

将推荐问题生成块（原 L330-356）**移到持久化之前**，紧接编排流结束后。并包裹子 span：

```python
final_output = "\n".join(final_output_parts).strip()

# ---- 编排流结束立即生成推荐问题（前置） ----
try:
    user_input_for_rec = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                user_input_for_rec = "\n".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                user_input_for_rec = str(content)
            break

    with langfuse_service.start_span(
        "recommended-questions",
        input={"user_input": user_input_for_rec, "reply": final_output},
    ) as rec_obs:
        questions = await _generate_recommended_questions(
            orchestrator_service, user_input_for_rec, final_output
        )
        if rec_obs:
            try:
                rec_obs.update(output={"questions": questions})
            except Exception:
                pass

    rec_event = json.dumps(
        {"type": "recommended_questions", "questions": questions},
        ensure_ascii=False,
    )
    yield f"data: {rec_event}\n\n"
except Exception:
    logger.debug("[chat_service] 推荐问题事件发送失败", exc_info=True)

# ---- 持久化对话历史（原 L244-306 块，整体下移到这里） ----
...

# ---- 文件检测 + yield files_generated（原 L308-328 块） ----
...

# ---- trace end + yield trace_ready（原 L358-376 块） ----
...
```

> 推荐问题生成失败仍不影响后续持久化/trace（已有 try/except 兜底）。

### 3.3 修改 `app/services/orchestrator_service.py`

#### (a) state_dict 加载后截断 context

L494-498（单 agent 路径）和 L621-625（多 agent 路径）加载 state_dict 后，截断 `context` 字段为最后 6 条。封装一个静态辅助方法：

```python
@staticmethod
def _trim_state_context(state_dict: dict, keep_last: int = 6) -> dict:
    """截断 AgentState.context 为最后 N 条消息，控制模型输入 token。

    AgentState.context 是完整对话历史 Msg_dict 列表；大模型上下文有限时
    仅保留最近 keep_last 条（默认 6 = 3 轮）。
    """
    if not state_dict:
        return state_dict
    ctx = state_dict.get("context")
    if isinstance(ctx, list) and len(ctx) > keep_last:
        state_dict = {**state_dict, "context": ctx[-keep_last:]}
    return state_dict
```

单 agent 路径（L497）：
```python
state_dict = await session_service.load_agent_state(session_id, agent_id)
if state_dict:
    state_dict = self._trim_state_context(state_dict)  # 新增
    agent_state = AgentState.model_validate(state_dict)
```

多 agent 路径（L624）：
```python
state_dict = await session_service.load_agent_state(session_id, aid)
if state_dict:
    state_dict = self._trim_state_context(state_dict)  # 新增
    agent_states[aid] = AgentState.model_validate(state_dict)
```

#### (b) 环节子 span 埋点

`OrchestratorService` 需持有 `langfuse_service` 引用才能在各环节起子 span。当前 `run()` 无此参数。新增可选参数 `langfuse_service`（默认 None，向后兼容）：

```python
async def run(
    self,
    messages: List[Dict[str, Any]],
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_service: Optional[Any] = None,
    agent_id: Optional[str] = None,
    request: Optional[Request] = None,
    search_enabled: bool = True,
    langfuse_service: Optional[Any] = None,  # 新增
) -> AsyncGenerator[str, None]:
```

在 `chat_service.generate_response` 调用 `orchestrator_service.run(...)` 时传入 `langfuse_service=langfuse_service`。

各环节埋点（用 `langfuse_service.start_span` 包裹）：

**改写**（L576-586）：
```python
with (langfuse_service.start_span("query-rewrite", input={"original": user_input, "history_len": len(history)}) if langfuse_service else _noop_ctx()) as rw_obs:
    try:
        rewritten = await rewriter.rewrite(user_input, history)
    except Exception:
        logger.exception("[OrchestratorService] 查询改写失败，使用原始输入")
        rewritten = user_input
    if rw_obs:
        try: rw_obs.update(output={"rewritten": rewritten})
        except Exception: pass
```

**意图识别**（L588-609）：
```python
with (langfuse_service.start_span("intent-recognition", input={"rewritten": rewritten, "history_len": len(history)}) if langfuse_service else _noop_ctx()) as ir_obs:
    try:
        intent_result = await recognizer.recognize(rewritten, history)
    except Exception:
        ...
    if ir_obs:
        try: ir_obs.update(output={"intents": [...], "relation": intent_result.relation})
        except Exception: pass
```

**每一次智能体调用**：在 `_run_single_agent`（[base.py:50-122](file:///workspace/app/orchestrator/base.py)）内包裹。需给 `_run_single_agent` 传 `langfuse_service`。由于 base.py 的 `_run_single_agent` 签名已有 `intent`，新增可选参数 `langfuse_service`，span name 用 `f"agent-{agent_id}"`，input 记 `{"intent": intent.id, "query": intent.query}`，output 记 `{"output": result.output, "success": result.success}`。

各编排器（pipeline/parallel/react）调用 `_run_single_agent` 时透传 `langfuse_service`。

**单 agent 直接问答路径**（L517-573）：在 `agent.reply_stream` 外包裹 `langfuse_service.start_span(f"agent-{agent_id}", ...)`。

> `_noop_ctx()`：当 langfuse_service 为 None 时返回一个 yield None 的空 context manager，避免每处都写 if/else。定义为模块级辅助：
> ```python
> from contextlib import contextmanager
> @contextmanager
> def _noop_ctx():
>     yield None
> ```

### 3.4 修改 `app/orchestrator/base.py` + 三个编排器

- `base.py` `_run_single_agent` 新增 `langfuse_service: Optional[Any] = None` 参数，包裹 `agent.reply_stream` 循环。
- `pipeline.py` / `parallel.py` / `react.py` 的 `run()` 新增 `langfuse_service` 参数，透传给 `_run_single_agent`。
- `orchestrator_service.py` L634 `orchestrator.run(intent_result, session_id=..., agent_states=...)` 传入 `langfuse_service=langfuse_service`。

## 四、假设与决策

1. **历史限 3 轮 = 6 条消息**：user+assistant 各 1 条为 1 轮，3 轮 = 6 条。截断用 `[-6:]`。
2. **截断点在加载后、使用前**：chat_service 的 history_messages、orchestrator 的 state_dict.context 在加载后立即截断；recognizer/rewriter 内部 -6 保持不动。dao 层不改（保持全量持久化，只是加载使用时截断）。
3. **state_dict 截断不落库**：只在内存截断 context 传给 AgentState，save_agent_state 仍存完整 state（由编排器执行后 `agent.state.model_dump()` 产生）。这样历史不丢失，仅控制输入 token。> 副作用：agent 执行后 state 会重新累积完整 context（本轮 + 截断后的历史），下次加载再截断。可接受。
4. **Langfuse v3 `start_as_current_observation`**：项目用 `langfuse>=3.5.0`（v3 GA），支持 OTel 自动嵌套。根 span + 子 span 都用此方法，context manager 包裹。
5. **根 span 用 context manager 包裹整个 generate_response 主体**：async generator 内 `with` 包裹 `yield` 合法，context 在 yield 期间保持活跃，子 span 在 orchestrator_service.run 内启动时能嵌套到根 span。
6. **推荐问题前置到编排流结束后、持久化前**：前端能更快拿到推荐问题；持久化/文件检测/trace 顺序不变，仍在后。
7. **langfuse_service 为 None 时全降级**：所有 span 调用用 `_noop_ctx()` 或 `if langfuse_service` 保护，无 langfuse 时行为与现状一致。
8. **单 agent 直接问答路径也埋点**：用户原话"每一次智能体调用"涵盖此路径；多选未勾选该项但语义包含，按原需求实现。
9. **不改 dao 层 SQL**：不在数据库层加 LIMIT，保持 load_messages/load_agent_state 全量返回，截断在服务层（更灵活，未来可调）。

## 五、验证步骤

1. **历史截断**：构造一个 >6 条历史的 session，`/chat` 发问，确认：
   - chat_service 传给 orchestrator 的 `full_messages` 历史部分只有 6 条（加日志断点）
   - orchestrator 传给 AgentState 的 context 只有 6 条
   - recognizer/rewriter 收到的 history 仍为 6 条（不变）
2. **Langfuse 子 span**：启用 langfuse，`/chat` 发问，在 Langfuse 面板确认 trace 下有嵌套子 span：`chat-response`（根）→ `query-rewrite` / `intent-recognition` / `agent-<id>` / `recommended-questions`，各 span 有 input/output 和耗时。
3. **单 agent 路径埋点**：带 `agent_id` 参数调 `/chat`，确认 trace 下有 `agent-<id>` span。
4. **推荐问题时机**：`/chat` 流式响应中，确认 `recommended_questions` 事件在 `summary` 之后很快到达，且在 `files_generated` / `trace_ready` 之前；持久化历史仍正常（下次加载历史含本轮）。
5. **无 langfuse 降级**：清空 LANGFUSE 配置，`/chat` 正常工作无异常。
6. **token 不超**：长历史 session 下，模型输入不超 20K token（可通过 langfuse 的 input token 数观察）。
7. **语法**：`python -c "import ast; ast.parse(open('app/services/chat_service.py').read()); ast.parse(open('app/services/orchestrator_service.py').read()); ast.parse(open('app/services/langfuse_service.py').read()); ast.parse(open('app/orchestrator/base.py').read())"`
8. **提交**：`git add` 修改文件 → commit → push `origin/trae/agent-5CYjia`。

## 六、执行顺序（实现阶段）

1. [app/services/langfuse_service.py](file:///workspace/app/services/langfuse_service.py)：新增 `start_span` context manager 方法
2. [app/services/orchestrator_service.py](file:///workspace/app/services/orchestrator_service.py)：
   - 新增 `_trim_state_context` 静态方法 + 模块级 `_noop_ctx`
   - 单 agent / 多 agent 路径 state_dict 截断
   - `run()` 新增 `langfuse_service` 参数
   - 改写/意图识别/单 agent 路径包裹子 span
   - `orchestrator.run(...)` 透传 langfuse_service
3. [app/orchestrator/base.py](file:///workspace/app/orchestrator/base.py)：`_run_single_agent` 新增 `langfuse_service` 参数 + 包裹 span
4. [app/orchestrator/pipeline.py](file:///workspace/app/orchestrator/pipeline.py) / [parallel.py](file:///workspace/app/orchestrator/parallel.py) / [react.py](file:///workspace/app/orchestrator/react.py)：`run()` 新增 `langfuse_service` 参数，透传给 `_run_single_agent`
5. [app/services/chat_service.py](file:///workspace/app/services/chat_service.py)：
   - history_messages 截断 `[-6:]`
   - 根 obs 改用 `start_span` context manager 包裹主体
   - 推荐问题生成块前置到编排流结束后、持久化前，并包裹 `recommended-questions` 子 span
   - `orchestrator_service.run(...)` 传入 `langfuse_service`
6. 语法校验
7. `git add` → commit → push `origin/trae/agent-5CYjia`
