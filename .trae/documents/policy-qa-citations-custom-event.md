# 将 policy_qa citations 作为自定义事件流返回前端

## Summary

policy_qa 工具当前把 `answer` + `citations` 格式化为文本放入 `ToolChunk` 返回给模型，导致 citations 的原始 JSON 结构丢失。本方案用 `contextvars.ContextVar` 在工具执行时捕获原始 citations，在 agent 事件循环结束后 emit 一个自定义 `policy_qa_citations` SSE 事件，前端可通过监听该事件类型获取完整的 citations JSON 对象。

## Current State Analysis

### 事件透传机制
- [app/orchestrator/base.py:114-122](file:///workspace/app/orchestrator/base.py#L114-L122)：`_run_single_agent` 中，`async for event in agent.reply_stream(user_msg)` 循环内，所有 `AgentEvent` 通过 `yield f"data: {event.model_dump_json()}\n\n"` 透传给前端。循环结束后 `yield result`（TaskResult）。
- [app/services/orchestrator_service.py:628-636](file:///workspace/app/services/orchestrator_service.py#L628-L636)：单 agent 短路路径 `_run_single_agent_path` 中有同样的事件透传逻辑。
- [app/orchestrator/base.py:163-165](file:///workspace/app/orchestrator/base.py#L163-L165)：`_event(data: dict) -> str` helper，将 dict 序列化为 SSE 字符串。

### policy_qa 工具现状
- [tools/policy_qa_tools.py](file:///workspace/tools/policy_qa_tools.py)：`create_policy_qa_tool` 返回的闭包工具，成功调用后把 `answer` + `_format_citations(citations)` 拼成文本放入 `ToolChunk`。citations 原始 JSON 仅在工具内部局部变量 `data` 中存在，函数返回后丢失。

### 两处需要 emit 的位置
1. [base.py:150](file:///workspace/app/orchestrator/base.py#L150)：`yield result` 之前（编排器路径：parallel/pipeline/react）
2. [orchestrator_service.py](file:///workspace/app/services/orchestrator_service.py#L646)：单 agent 路径 agent 执行完成后（`final_state` 保存后、`yield` 结束前）

## Proposed Changes

### 1. 修改 `tools/policy_qa_tools.py`（捕获 citations）

新增模块级 `ContextVar` 和 set/consume 函数；工具执行成功时把原始 citations 写入 ContextVar。

**新增内容**：
```python
from contextvars import ContextVar

# 捕获 policy_qa 工具执行期间的 citations，供 orchestrator emit 自定义事件
_policy_qa_citations: ContextVar[list] = ContextVar(
    "_policy_qa_citations", default_factory=list
)

def _set_citations(citations: list) -> None:
    """工具执行成功时调用，捕获原始 citations JSON。"""
    _policy_qa_citations.set(citations or [])

def consume_policy_qa_citations() -> list:
    """读取并清空捕获的 citations。

    供 orchestrator 在 agent 事件循环结束后调用，emit 自定义事件。
    返回空列表表示本次无 policy_qa 调用或无 citations。
    """
    try:
        citations = _policy_qa_citations.get()
        if citations:
            _policy_qa_citations.set([])
        return citations
    except Exception:
        return []
```

**修改 policy_qa 闭包内部**：在成功解析响应后、`return _build_result(content)` 之前，调用 `_set_citations(citations)`：
```python
# 4. 格式化输出：回答正文 + 引用来源
content = answer
citations_text = _format_citations(citations)
if citations_text:
    content += "\n\n--- 引用来源 ---\n" + citations_text

# 捕获原始 citations 供 orchestrator emit 自定义事件
_set_citations(citations)

return _build_result(content)
```

**注**：错误/无权限/未命中路径不调用 `_set_citations`（保持默认空列表），不会 emit 无意义事件。多次调用 policy_qa 时，ContextVar 后者覆盖前者（可接受，一次 reply 通常只调一次）。

### 2. 修改 `app/orchestrator/base.py`（编排器路径 emit）

新增模块级 helper 函数，在 `_run_single_agent` 的 `yield result` 前调用。

**新增 helper**（文件顶部 import 区附近）：
```python
def _emit_pending_tool_extras() -> list[str]:
    """检查并 emit 工具执行期间捕获的额外数据（如 policy_qa 的 citations）。

    返回 SSE 事件字符串列表（可能为空）。导入失败时静默跳过，
    保证 base.py 不硬依赖 policy_qa_tools 模块。
    """
    events: list[str] = []
    try:
        from tools.policy_qa_tools import consume_policy_qa_citations
        citations = consume_policy_qa_citations()
        if citations:
            events.append(
                f"data: {json.dumps({
                    'type': 'policy_qa_citations',
                    'citations': citations,
                }, ensure_ascii=False)}\n\n"
            )
    except Exception:
        pass
    return events
```

**修改 `_run_single_agent`**（L150 `yield result` 前）：
```python
            # emit 工具执行期间捕获的额外数据（如 policy_qa citations）
            for ev in _emit_pending_tool_extras():
                yield ev

        yield result
```

### 3. 修改 `app/services/orchestrator_service.py`（单 agent 路径 emit）

在单 agent 短路路径中，agent 执行完成后（`final_state` 保存后、`_safe_update_span` 前）emit。

**修改位置**：L646-648 附近（`await self._persist_agent_state(...)` 之后），插入：
```python
                # emit 工具执行期间捕获的额外数据（如 policy_qa citations）
                from app.orchestrator.base import _emit_pending_tool_extras
                for ev in _emit_pending_tool_extras():
                    yield ev
```

复用 base.py 的 helper，避免重复实现。

## 事件格式

前端收到的自定义事件：
```json
{
  "type": "policy_qa_citations",
  "citations": [
    {
      "position": 1,
      "dataset_id": "kb_xxx",
      "dataset_name": "机构准入制度库",
      "document_id": "doc_xxx",
      "document_name": "入网机构管理办法",
      "segment_id": "chunk_xxx",
      "score": 0.9523,
      "content": "入网机构应当具备以下技术条件……",
      "page_start": 12,
      "page_end": 13,
      "element_ids": [],
      "chunk_type": "article"
    }
  ]
}
```

前端只需监听 `type === "policy_qa_citations"` 即可获取完整 citations JSON 数组。

## Assumptions & Decisions

1. **ContextVar 方案**：`contextvars.ContextVar` 是 Python 标准库，协程安全，每个请求（async task）独立副本，无需额外传参。适合在工具深层执行时捕获数据、在外层 emit 的场景。
2. **emit 时机**：在 `reply_stream` 循环结束后 emit，确保 citations 在 agent 完整回复后才发送，时序明确。若 agent 一次 reply 多次调用 policy_qa，ContextVar 后者覆盖前者（一次 reply 通常只调一次，可接受）。
3. **不硬耦合**：base.py 的 `_emit_pending_tool_extras` 用 `try/except` 包裹导入，policy_qa_tools 不存在或导入失败时静默跳过，不影响主流程。未来其他工具也可复用此机制扩展。
4. **错误路径不 emit**：policy_qa 工具在无权限/未命中/异常路径不调用 `_set_citations`，ContextVar 保持默认空列表，`consume` 返回空，不 emit 无意义事件。
5. **citations 仍保留在 ToolChunk 文本中**：给模型看的格式化引用文本不变（模型需要引用来源生成回答），citations JSON 是**额外**发给前端的，不影响模型行为。
6. **两处 emit 点**：base.py（编排器路径）和 orchestrator_service.py（单 agent 路径）都需要处理，复用同一 helper。

## Verification

1. **语法校验**：`python -c "import ast; ast.parse(open('tools/policy_qa_tools.py').read()); ast.parse(open('app/orchestrator/base.py').read()); ast.parse(open('app/services/orchestrator_service.py').read()); print('OK')"`
2. **ContextVar 行为校验**：
   - `_set_citations([{...}])` 后 `consume_policy_qa_citations()` 返回该列表
   - consume 后再次 consume 返回空列表（已清空）
   - 未 set 时 consume 返回空列表
3. **事件流校验**（需运行环境）：
   - 用户有权限且知识库命中 → 前端收到 `policy_qa_citations` 事件，citations 为完整 JSON 数组
   - 用户无权限 → 前端不收到该事件（工具未 set citations）
   - 单 agent 路径和编排器路径均能 emit
4. **回归校验**：现有事件流（session_ready/orchestration_start/task_start/agent 事件/task_end/recommended_questions）不受影响
