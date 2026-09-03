# 排查：pipeline 执行失败时回答未持久化

## 结论（根因）

**`final_output` 为空导致 assistant 消息未入库。** 这是设计上的漏掉，不是异常吞掉。

## 完整证据链

### 1. 持久化只认 `summary` 事件

[chat_service.py:421-437](file:///workspace/app/services/chat_service.py#L421-L437) 中，`final_output_parts` **只**从 `type == "summary"` 事件收集：

```python
if event_type == "summary":
    final_output_parts.append(payload.get("content", ""))
...
final_output = "\n".join(final_output_parts).strip()
```

随后 [chat_service.py:446-449](file:///workspace/app/services/chat_service.py#L446) 调 `_persist_conversation_history(..., final_output)`。

### 2. `_persist_conversation_history` 对空 output 的处理

[chat_service.py:208-229](file:///workspace/app/services/chat_service.py#L208-L229)：

```python
if final_output:        # ← 空字符串为 False
    new_messages.append({"role": "assistant", ...})
if new_messages:
    await session_service.append_messages(session_id, user_id, new_messages)
```

- `final_output == ""` → `if final_output:` 为 False → **不追加 assistant 消息**
- `user_input` 非空 → 仍追加 user 消息
- 结果：**只落库了 user，没落库 assistant** ← 与你观察到的现象完全吻合

### 3. pipeline 失败路径不 yield summary

[pipeline.py:108-115](file:///workspace/app/orchestrator/pipeline.py#L108-L115)：

```python
if not result.success:
    yield self._event({
        "type": "pipeline_intercept",
        "step": i + 1,
        "intent_id": intent.id,
        "message": f"步骤 {i + 1}（{intent.id}）执行失败，流水线终止：{result.output}",
    })
    return              # ← 直接 return，后续 summary 不再 yield
```

失败时 yield 的是 `pipeline_intercept`（含 `result.output`），然后 `return`，**永不 yield `summary`**。

而 summary 只在全部成功时才 yield（[pipeline.py:120-124](file:///workspace/app/orchestrator/pipeline.py#L120-L124)）：

```python
# 流水线全部完成
yield self._event({"type": "summary", "content": prior_context.strip()})
```

### 4. 横向对比：parallel / 单 agent 路径有 summary 兜底

- **parallel**（[parallel.py:137-147](file:///workspace/app/orchestrator/parallel.py#L137)）：无论单条成功与否，只要 `summary_parts` 非空就 yield summary → 即使部分失败也有 output。
- **单 agent 路径**（[orchestrator_service.py:629-633](file:///workspace/app/services/orchestrator_service.py#L629) 附近）：最后必 yield summary。
- **react**：失败时 yield `react_final`（[react.py:117,237](file:///workspace/app/orchestrator/react.py#L117)），但 chat_service **只认 `summary`** → react 失败时同样不会持久化 assistant（这是同类问题，本次排查一并发现）。

### 5. 关键确认：失败时并非无可用文本

失败场景下其实**有文本**，只是没走 summary 通道：
- `pipeline_intercept` 的 `message` 含 `result.output`（失败步的输出，可能为 "步骤执行超时" 或 agent 已产出的部分文本）
- `task_end` 事件的 `success: false`
- `result.output` 本身（agent 执行异常时为 "执行出错: ..."，超时时为 "步骤执行超时"）

但这些事件类型都不是 `summary`，`final_output_parts` 收集不到。

## 受影响范围

| 编排器 | 失败时是否持久化 assistant | 原因 |
|---|---|---|
| pipeline | **否** | 失败 `return` 前 only yield `pipeline_intercept`，无 summary |
| react | **否**（同类问题） | 失败 yield `react_final`，chat_service 只认 `summary` |
| parallel | 是（部分场景） | 部分失败时 `summary_parts` 仍可能非空 → yield summary；但全部失败且 output 全空时也会漏 |
| 单 agent | 是 | 始终 yield summary（即使异常路径） |

## 设计层面的取舍点（待决策，本次不改）

要让失败场景也持久化 assistant，存在几种思路，各有取舍：

1. **chat_service 扩充收集来源**：除 `summary` 外，也收集 `pipeline_intercept` / `react_final` 的文本作为 `final_output` 兜底。
   - 优点：改动集中在一处，对所有编排器统一生效。
   - 风险：`final_output` 语义从"最终回答"变成"最终回答或失败说明"，前端推荐问题生成、根 span output 都会受影响。

2. **pipeline 失败时也 yield summary**：在 `pipeline_intercept` 后、`return` 前，把已有输出（失败步 output 或 prior_context）yield 一个 `summary`。
   - 优点：保持 `final_output = summary` 的纯净语义。
   - 风险：前端收到 `pipeline_intercept` 后又收到 `summary`，需确认前端是否按 summary 渲染最终回答。

3. **`_persist_conversation_history` 改为即使 output 空也落 assistant（标记 success=False）**：
   - 优点：保证每轮都有 assistant 记录，便于审计。
   - 风险：空 content 的 assistant 消息对前端历史回看无意义。

## 验证步骤（本次仅排查，不执行改动）

1. 复现：构造一个会触发 `pipeline_intercept` 的请求（如让某步 agent 抛异常或超时）。
2. 查库：`SELECT role, content, success FROM messages WHERE session_id = ? ORDER BY id`。
3. 预期：只有 `role='user'` 一条，无 `role='assistant'`。✅ 与现象吻合。

## 不改动的部分

本次为纯排查，不修改任何代码。
