# 在 Langfuse span 中记录提示词

## Summary

当前 5 个环节（问题改写、意图识别、意图编排、agent 回复、推荐问题生成）的 Langfuse span 只记录输入参数，未记录实际发送给 LLM 的提示词。本方案让各环节把构造好的 `system_prompt` / `user_prompt` 回传给调用方，调用方将其写入 span 的 input，实现提示词在 Langfuse 监控中可观测。

## Current State Analysis

### 5 个环节的 span 与提示词现状

1. **query-rewrite** — [orchestrator_service.py:746-748](file:///workspace/app/services/orchestrator_service.py#L746)
   - span input: `{"original", "history_len"}`
   - 提示词在 [rewriter.py:74-89](file:///workspace/app/intent/rewriter.py#L74-L89)：`system_prompt`（`self._rewrite_prompt` 替换 `{{current_date}}`）+ `user_prompt`（历史上下文 + 用户输入）
   - `rewrite()` 只返回 `str`，提示词丢失

2. **intent-recognition** — [orchestrator_service.py:768-770](file:///workspace/app/services/orchestrator_service.py#L768)
   - span input: `{"query", "history_len"}`
   - 提示词在 [recognizer.py:108-134](file:///workspace/app/intent/recognizer.py#L108-L134)：`_build_recognition_prompt` 构造 user_prompt（含 `{{intents}}` + 历史）+ 硬编码 system_prompt
   - `recognize_intents()` 只返回 `List[Intent]`，提示词丢失

3. **intent-orchestration** — [orchestrator_service.py:790-792](file:///workspace/app/services/orchestrator_service.py#L790)
   - span input: `{"intents_count"}`
   - 提示词在 [recognizer.py:179-200](file:///workspace/app/intent/recognizer.py#L179-L200)：`_build_orchestration_prompt` 构造 user_prompt（含 `{{intents_list}}`）+ 硬编码 system_prompt
   - `plan_orchestration()` 只返回 `Tuple[str, List[int]]`，提示词丢失

4. **agent reply** — [base.py:128-135](file:///workspace/app/orchestrator/base.py#L128)
   - span input: `{"intent", "query"}`
   - agent 的 system_prompt 在创建时配置（来自 model_config.yml prompts），未记录到 span
   - agentscope agent 通过 `agent.sys_prompt` 属性暴露 system_prompt

5. **recommended-questions** — [chat_service.py:150-153](file:///workspace/app/services/chat_service.py#L150)
   - span input: `{"user_input", "reply"}`
   - 提示词在 [chat_service.py:64-72](file:///workspace/app/services/chat_service.py#L64-L72)：硬编码 system_prompt + `user_prompt`
   - `_generate_recommended_questions()` 只返回 `List[str]`，提示词丢失

## Proposed Changes

### 1. 修改 `app/intent/rewriter.py`（query-rewrite 回传提示词）

`rewrite()` 返回值从 `str` 改为 `Tuple[str, dict]`，第二个元素为提示词 dict。

**修改 `rewrite` 方法签名和返回**：
```python
async def rewrite(self, user_input: str, history: Optional[List[dict]] = None) -> Tuple[str, dict]:
    """改写用户输入。

    Returns:
        (改写后的查询, {"system_prompt": ..., "user_prompt": ...})；
        无需改写时返回 (原始输入, {})。
    """
```

- 无上下文且不含时间代词的快速返回路径：`return user_input, {}`
- 改写成功路径：`return rewritten, {"system_prompt": system_prompt, "user_prompt": user_prompt}`
- 异常降级路径：`return user_input, {}`

### 2. 修改 `app/intent/recognizer.py`（intent-recognition / orchestration 回传提示词）

`recognize_intents()` 和 `plan_orchestration()` 返回值增加提示词 dict。

**`recognize_intents` 改为返回 `Tuple[List[Intent], dict]`**：
- 成功：`return self._parse_intents(raw_json, user_input), prompts`
- 降级：`return [Intent(...)], {}`
- prompts 在 `_call_recognition_llm` 内构造，改为返回 `(dict, prompts)` 或在 `recognize_intents` 中调用 `_build_recognition_prompt` 获取 user_prompt

**`plan_orchestration` 改为返回 `Tuple[str, List[int], dict]`**：
- 成功：`return (relation, execution_order, prompts)`
- 降级：`return ("independent", [], {})`

**`recognize()` 兼容方法**：内部解包两步返回值，适配新签名。

**实现方式**：`_call_recognition_llm` / `_call_orchestration_llm` 改为返回 `(raw_json, prompts)`，其中 prompts = `{"system_prompt": ..., "user_prompt": ...}`。

### 3. 修改 `app/services/orchestrator_service.py`（3 处 span 写入提示词）

**query-rewrite span**（L746-754）：
```python
with self._span(
    langfuse_service, "query-rewrite",
    {"original": user_input, "history_len": len(history)},
) as rw_span:
    try:
        rewritten, rw_prompts = await rewriter.rewrite(user_input, history)
    except Exception:
        logger.exception("[OrchestratorService] 查询改写失败，使用原始输入")
        rewritten, rw_prompts = user_input, {}
    _safe_update_span(rw_span, {
        "rewritten": rewritten,
        "prompts": rw_prompts,
    })
```

**intent-recognition span**（L768-778）：
```python
with self._span(
    langfuse_service, "intent-recognition",
    {"query": rewritten, "history_len": len(history)},
) as rec_span:
    try:
        intents, rec_prompts = await recognizer.recognize_intents(rewritten, history)
    except Exception:
        logger.exception("[OrchestratorService] 意图识别失败，降级为 general_chat")
        intents = [Intent(id="general_chat", query=rewritten, agent="general_agent")]
        rec_prompts = {}
    _safe_update_span(rec_span, {
        "intents": [{"id": i.id, "agent": i.agent} for i in intents],
        "prompts": rec_prompts,
    })
```

**intent-orchestration span**（L790-800）：
```python
with self._span(
    langfuse_service, "intent-orchestration",
    {"intents_count": len(intents)},
) as orch_span:
    try:
        relation, execution_order, orch_prompts = await recognizer.plan_orchestration(rewritten, intents)
    except Exception:
        logger.exception("[OrchestratorService] 意图编排失败，降级为 independent")
        relation, execution_order, orch_prompts = "independent", [], {}
    _safe_update_span(orch_span, {
        "relation": relation, "execution_order": execution_order,
        "prompts": orch_prompts,
    })
```

**注意**：prompts 放在 span 的 output（通过 `_safe_update_span`），因为 input 在 span 创建时还没构造提示词。Langfuse span 的 output 同样在监控节点可见。

### 4. 修改 `app/orchestrator/base.py`（agent span 记录 system_prompt）

在 agent span 的 input 中增加 agent 的 system_prompt。

**修改 L128-135**：
```python
# 从 agent 对象读取 system_prompt（agentscope ReActAgent 通过 sys_prompt 属性暴露）
agent_sys_prompt = getattr(agent, "sys_prompt", None) or ""

span_ctx = (
    langfuse_service.start_span(
        f"agent-{agent_id}",
        input={
            "intent": intent.id,
            "query": intent.query,
            "system_prompt": agent_sys_prompt,
            "user_message": user_content,
        },
    )
    if langfuse_service
    else _noop_ctx()
)
```

### 5. 修改 `app/services/chat_service.py`（recommended-questions span 记录提示词）

`_generate_recommended_questions` 返回值从 `List[str]` 改为 `Tuple[List[str], dict]`。

**修改 `_generate_recommended_questions`**：
```python
async def _generate_recommended_questions(
    orchestrator_service, user_input: str, final_output: str,
) -> Tuple[List[str], dict]:
    ...
    system_prompt = (...)
    user_prompt = f"用户提问：{user_input}\n\n助手回答：{final_output}"
    text = await chat_complete(client, model_config, system_prompt, user_prompt)
    ...
    return cleaned[:3], {"system_prompt": system_prompt, "user_prompt": user_prompt}
```

**修改 `_emit_recommended_questions`**（L157-161）：
```python
with root_span_ctx as rec_obs:
    questions, rq_prompts = await _generate_recommended_questions(
        orchestrator_service, user_input, final_output
    )
    _safe_update_span(rec_obs, {"questions": questions, "prompts": rq_prompts})
```

## Assumptions & Decisions

1. **提示词记录到 span output 而非 input**：query-rewrite/intent-recognition/intent-orchestration 的提示词在方法调用后才构造完成，span 创建时（input）还没有提示词。因此通过 `_safe_update_span` 写入 output，Langfuse 监控节点同样可见。recommended-questions 同理。agent span 的 system_prompt 在 span 创建时即可获取（agent 已创建），放入 input。
2. **返回值携带提示词**：让 rewriter/recognizer 返回提示词 dict，调用方写入 span。显式清晰，无线程安全问题。不改 ContextVar（提示词非敏感数据，无需旁路隐藏）。
3. **agent system_prompt 来源**：agentscope ReActAgent 通过 `agent.sys_prompt` 属性暴露 system_prompt，用 `getattr(agent, "sys_prompt", None)` 安全读取，不存在时为空字符串。
4. **降级路径返回空 dict**：各环节降级时不构造提示词，返回 `{}`，span 中 prompts 为空对象，不影响可观测性。
5. **prompts dict 结构统一**：`{"system_prompt": str, "user_prompt": str}`，5 个环节一致，便于 Langfuse 中按结构化查看。
6. **不改 span name**：保持现有 `query-rewrite` / `intent-recognition` / `intent-orchestration` / `agent-{agent_id}` / `recommended-questions` 命名，避免影响现有监控看板。

## Verification

1. **语法校验**：`python -c "import ast; [ast.parse(open(f).read(), filename=f) for f in ['app/intent/rewriter.py', 'app/intent/recognizer.py', 'app/services/orchestrator_service.py', 'app/orchestrator/base.py', 'app/services/chat_service.py']]; print('OK')"`
2. **返回值契约校验**：
   - `rewriter.rewrite()` 返回 `(str, dict)`
   - `recognizer.recognize_intents()` 返回 `(List[Intent], dict)`
   - `recognizer.plan_orchestration()` 返回 `(str, List[int], dict)`
   - `_generate_recommended_questions()` 返回 `(List[str], dict)`
   - `recognizer.recognize()` 内部正确解包两步返回值
3. **span 内容校验**（需运行环境）：
   - query-rewrite span output 含 `prompts.system_prompt`（含替换后的 `{{current_date}}`）+ `prompts.user_prompt`（含历史上下文）
   - intent-recognition span output 含 `prompts.system_prompt` + `prompts.user_prompt`（含 `{{intents}}` 替换后的意图清单）
   - intent-orchestration span output 含 `prompts.system_prompt` + `prompts.user_prompt`（含 `{{intents_list}}`）
   - agent span input 含 `system_prompt`（agent 的系统提示词）+ `user_message`
   - recommended-questions span output 含 `prompts.system_prompt` + `prompts.user_prompt`
4. **回归校验**：降级路径（无上下文/异常）返回空 dict，不报错；现有事件流和持久化逻辑不受影响
