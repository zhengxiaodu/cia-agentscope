# 引用来源改走 ToolChunk.metadata 传递

## Summary

把制度问答（policy_qa）工具的引用来源（citations）从 **ContextVar 旁路传递** 改为 **ToolChunk.metadata 携带 → 框架自动透传到 ToolResultEndEvent.metadata → 旁路提取 emit 自定义事件流**。

移除 `tools/policy_qa_tools.py` 中的 ContextVar（`_policy_qa_citations` / `_set_citations` / `consume_policy_qa_citations`），改为在 `AgentEventTracer` 处理 `ToolResultEndEvent` 时从 `event.metadata` 累积 citations，agent 事件流结束后由调用方 emit SSE 事件。

## Current State Analysis

现状数据流（ContextVar 旁路）：
1. `tools/policy_qa_tools.py:229` `_set_citations(citations)` 写入 ContextVar
2. `app/orchestrator/base.py:34` `consume_policy_qa_citations()` 读取并清空 ContextVar
3. `app/orchestrator/base.py:37-44` 构造 `{"type": "policy_qa_citations", "citations": [...]}` SSE
4. `base.py:189-191`（编排器路径）和 `orchestrator_service.py:689-692`（单 agent 短路路径）调用 `_emit_pending_tool_extras()` emit

问题：ContextVar 是隐式全局旁路，工具与 emit 点之间耦合在模块级状态上，难以测试、难以复用、跨工具泛化困难。

## 关键框架行为确认（Phase 1 已验证）

1. **ToolChunk 有 metadata 字段**（`agentscope/tool/_response.py`）：
   ```python
   metadata: dict = Field(default_factory=dict)
   """The metadata to be accessed within the agent, so that we don't need to
   parse the tool result block."""
   ```
   设计意图正是"在 agent 内部访问，无需解析 tool result block"。

2. **agentscope 自动把 ToolChunk.metadata 透传到 ToolResultEndEvent.metadata**（`agentscope/agent/_agent.py:2315-2320`，主路径）：
   ```python
   yield ToolResultEndEvent(
       reply_id=self.state.reply_id,
       tool_call_id=tool_call.id,
       state=chunk.state,
       metadata=chunk.metadata,   # ← 直接透传
   )
   ```
   注：中断路径（L745）和不带 metadata 的路径（L2480）会得到默认空 dict，不影响。

3. **ToolResultEndEvent 是 AgentEvent 子类**，会流过 `iter_agent_events` → 被 `agent_event_tracer.on_event` 处理 → 被 yield 给上层。`agent_event_tracer.py:102-106` 已处理该事件（目前只读 `event.state`，没碰 `event.metadata`），是天然的旁路累积点。

## Proposed Changes

### 文件 1: `tools/policy_qa_tools.py`

**移除**：
- L14 `from contextvars import ContextVar`
- L26-30 `_policy_qa_citations` ContextVar 定义
- L33-35 `_set_citations` 函数
- L38-51 `consume_policy_qa_citations` 函数

**修改 `_build_result`**（L143-145）：接收 `citations` 参数，写入 `ToolChunk.metadata`：
```python
def _build_result(text: str, citations: list = None) -> ToolChunk:
    """构造 ToolChunk 结果。

    citations 写入 metadata，agentscope 会自动透传到
    ToolResultEndEvent.metadata，由 AgentEventTracer 旁路提取。
    """
    metadata = {"citations": citations} if citations else {}
    return ToolChunk(
        content=[TextBlock(text=text)],
        is_last=True,
        metadata=metadata,
    )
```

**修改闭包内调用点**（L218-231）：把 `_set_citations(citations)` 删除，改为传给 `_build_result`：
```python
            if not answer:
                return _build_result("未检索到相关内容，无法回答。")

            content = answer
            citations_text = _format_citations(citations)
            if citations_text:
                content += "\n\n--- 引用来源 ---\n" + citations_text

            # citations 写入 ToolChunk.metadata，由框架透传到事件层
            return _build_result(content, citations=citations)
```
（早期返回分支 `_build_result("未检索到相关内容...")` 不传 citations，保持不变。）

### 文件 2: `app/utils/agent_event_tracer.py`

**新增累积字段**（`__init__`，约 L40 附近）：
```python
self._collected_citations: list = []
```

**修改 `on_event` 的 `ToolResultEndEvent` 分支**（L102-106）：在现有 `state` 处理后，提取 `event.metadata.get("citations")` 累积：
```python
        elif isinstance(event, ToolResultEndEvent):
            state = getattr(event.state, "value", str(event.state))
            if state == "error":
                self.tool_failures += 1
            self._end_tool(event.tool_call_id, state)
            # 从 metadata 提取 citations（policy_qa 等工具写入）
            meta_citations = (event.metadata or {}).get("citations")
            if isinstance(meta_citations, list) and meta_citations:
                self._collected_citations.extend(meta_citations)
```

**新增 `consume_citations` 方法**（在 `summary` 方法附近，约 L130 后）：
```python
    def consume_citations(self) -> list:
        """返回并清空累积的 citations（供调用方 emit 自定义事件流）。

        在 agent 事件流结束后调用；tracer.close() 不影响此方法
        （close 只关闭 langfuse observation，不清空 citations）。
        """
        if not self._collected_citations:
            return []
        result = self._collected_citations
        self._collected_citations = []
        return result
```

### 文件 3: `app/orchestrator/base.py`

**移除 `_emit_pending_tool_extras` 函数**（L26-47）：整个函数删除（不再依赖 ContextVar）。

**修改 `_run_single_agent` 末尾 emit**（L189-191）：改为从 tracer 提取 citations。
注意 `tracer` 是 L147 的局部变量，L177 `finally: tracer.close()` 已执行但 `consume_citations` 不受影响。改为：
```python
        # emit 工具执行期间捕获的 citations（从 ToolResultEndEvent.metadata 累积）
        citations = tracer.consume_citations()
        if citations:
            yield (
                "data: "
                + json.dumps(
                    {"type": "policy_qa_citations", "citations": citations},
                    ensure_ascii=False,
                )
                + "\n\n"
            )

        yield result
```

### 文件 4: `app/services/orchestrator_service.py`

**修改单 agent 短路路径 emit**（L689-692）：与 base.py 同样改为从 tracer 提取。该路径内 `tracer` 也是局部变量（L671 附近 `iter_agent_events(agent, user_msg, tracer, ...)`），在 `tracer.close()` 后调用 `consume_citations`：
```python
                # emit 工具执行期间捕获的 citations（从 ToolResultEndEvent.metadata 累积）
                citations = tracer.consume_citations()
                if citations:
                    yield (
                        "data: "
                        + json.dumps(
                            {"type": "policy_qa_citations",
                             "citations": citations},
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
```
删除原 `from app.orchestrator.base import _emit_pending_tool_extras` 导入（L690）。

## Assumptions & Decisions

1. **完全移除 ContextVar 方案**：不保留兜底。`ToolChunk.metadata → ToolResultEndEvent.metadata` 路径已完整覆盖原功能，ContextVar 是冗余旁路，移除可消除隐式全局状态。
2. **SSE 事件 schema 保持不变**：`{"type": "policy_qa_citations", "citations": [...]}` 不变，前端契约零改动。
3. **累积时机：批量（agent 流结束后统一 emit）**：与现状行为一致，不改成实时。`ToolResultEndEvent` 到达时只累积不 emit，避免中途 emit 造成的并发与前端时序问题。
4. **提取逻辑泛化但不主动扩展其他工具**：`on_event` 提取的是 `event.metadata.get("citations")`，任何工具把 citations 写进 metadata 都能被提取，这是自然泛化。但本次只改 policy_qa 一个工具，不主动给其他工具加 citations。
5. **字段名以 agentscope 实际定义为准**：用户说的"meta_data"实际是 `metadata`（`ToolChunk.metadata` / `ToolResultEndEvent.metadata`），按框架实际字段名实现。
6. **tracer.close() 不清空 citations**：close 只负责关闭 langfuse observation，citations 累积数据保留至 `consume_citations` 被调用。这样 L189 emit 可在 `finally: tracer.close()`（L177）之后安全调用。

## Verification Steps

1. **单元测试**：在 `tests/test_agent_event_tracer.py`（如不存在则新建）增加用例：
   - 构造带 `metadata={"citations": [...]}` 的 `ToolResultEndEvent` 喂给 tracer，`consume_citations()` 返回该列表
   - 不带 metadata 的事件不影响累积
   - `consume_citations` 调用后清空（二次调用返回空）
2. **policy_qa 工具测试**：在 `tests/test_policy_qa_tools.py` 增加用例：
   - `_build_result(text, citations=[...])` 返回的 `ToolChunk.metadata["citations"]` 等于传入列表
   - `_build_result(text)`（无 citations）的 metadata 为空 dict
3. **回归测试**：`pytest tests/` 全量通过（当前基线 62 个测试）。
4. **端到端冒烟（手动）**：发起一轮制度问答请求，确认前端仍收到 `policy_qa_citations` 事件且 citations 内容完整（含 content 字段）。
