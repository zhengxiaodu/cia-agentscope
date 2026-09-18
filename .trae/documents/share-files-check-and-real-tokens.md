# 分享详情文件字段核查 + 真实 token 消耗持久化 改造计划

## Summary

两个改造点：

1. **分享详情接口文件字段核查**：经核实 `GET /message_share/{shared_id}` **已经返回** `files`（系统产出文件）与 `upload_files`（用户上传文件）两个列表并按 message_pair_id 过滤，**无需代码改动**（本计划仅记录核查结论）
2. **真实 token 消耗持久化**：把 `AgentEventTracer` 捕捉的 `inputTokens + outputTokens` 之和作为本轮 token 消耗持久化到 messages 表，替代 `_estimate_tokens` 简单估算；同时把 token 累积从 langfuse 启用门控中解耦（langfuse 关闭时也能捕捉）

## Current State Analysis

### 改造点 1：分享详情已有文件字段（核查结论，无改动）

- [message_share.py L100-107](file:///workspace/app/routes/message_share.py#L100-L107)：`get_session_detail` 返回的 detail 中 `files` / `upload_files` 列表**已经**按 `message_pair_id ∈ 分享的 pair 集合` 过滤并随响应返回；`messages` 同样过滤
- SessionDetailResponse（[session.py L53-L60](file:///workspace/app/models/session.py#L53-L60)）本身就含 `files: List[SessionFile]` 与 `upload_files: List[SessionUploadFile]`，model_dump 后完整输出
- 现有测试已覆盖：test_message_share.py `test_get_share_no_auth_required_and_filters` 断言了 `data["files"]` 只含 p1 的文件、`data["upload_files"]` 剔除了 p2 的上传文件

### 改造点 2：token 估算 → 真实值

当前链路与问题：

- [chat_service.py L266](file:///workspace/app/services/chat_service.py#L266)：assistant 消息 `tokens` 用 `_estimate_tokens(final_output)`（文本长度估算，L46）
- [agent_event_tracer.py L139-140](file:///workspace/app/utils/agent_event_tracer.py#L139-L140)：`input_tokens` / `output_tokens` 在 `ModelCallEndEvent` 时累积——但累积逻辑在 `_dispatch` 内，而 `on_event` 中 `_dispatch` 受 `_enabled`（langfuse 启用）门控（L57-58），**langfuse 关闭时 token 恒为 0**
- tracer 是局部对象，token 数据目前只进 langfuse span output（`tracer.summary()`），**没有任何通道传到 chat_service**
- 两条 agent 执行路径：
  - **编排模式**（多 agent）：[base.py L123](file:///workspace/app/orchestrator/base.py#L123) 每 agent 一个 tracer；TaskResult（L26-43）无 token 字段，token 随局部 tracer 丢弃；执行后 [orchestrator_service.py L1222-1224](file:///workspace/app/services/orchestrator_service.py#L1222-L1224) 从 `orchestrator._last_results` 汇总 agent_ids（汇总模式可复用）
  - **单 agent 模式**：[orchestrator_service.py L756](file:///workspace/app/services/orchestrator_service.py#L756) 局部 tracer；L813-814 设置 `self._last_agent_ids` / `self._last_success`（属性传递模式可复用，chat_service L249-260 已在读）
- 现有传递先例：`last_agent_ids` / `last_success` 属性（定义于 orchestrator_service.py L141-143 初始化 + L232-249 property）

## Proposed Changes

### 1. agent_event_tracer.py：token 累积解耦 langfuse 门控

`on_event` 中新增 `_collect_tokens(event)`，置于 `_enabled` 门控**之前**（与现有 `_collect_citations` 完全同模式）：

```python
def on_event(self, event: Any) -> None:
    self._collect_citations(event)
    self._collect_tokens(event)          # 新增：先于埋点逻辑，不依赖 langfuse
    if not self._enabled:
        return
    ...

def _collect_tokens(self, event: Any) -> None:
    """从 ModelCallEndEvent 累积真实 token 用量（不依赖 langfuse 启用状态）。"""
    try:
        if isinstance(event, ModelCallEndEvent):
            self.input_tokens += event.input_tokens or 0
            self.output_tokens += event.output_tokens or 0
    except Exception:
        logger.debug("[AgentEventTracer] 累积 token 失败", exc_info=True)
```

同时从 `_dispatch` 的 `ModelCallEndEvent` 分支（L138-140）中**删除**重复的 token 累积两行（保留 `_end_model` 调用与 llm_calls 计数）。

### 2. base.py：TaskResult 携带 token（编排模式传递通道）

- TaskResult（[base.py L26-43](file:///workspace/app/orchestrator/base.py#L26-L43)）新增字段：`input_tokens: int = 0`、`output_tokens: int = 0`
- `_run_single_agent` 的 `finally: tracer.close()`（L152-153）中写入：

```python
finally:
    result.input_tokens = tracer.input_tokens
    result.output_tokens = tracer.output_tokens
    tracer.close()
```

成功/异常路径均覆盖（agent 创建失败路径 L86-92 无 tracer，字段默认 0）。

### 3. orchestrator_service.py：last_input_tokens / last_output_tokens 属性

- `__init__`（L141-143 附近）新增：`self._last_input_tokens: int = 0`、`self._last_output_tokens: int = 0`
- property 区（L232-249 附近，紧随 last_success）新增：

```python
@property
def last_input_tokens(self) -> int:
    """最近一轮编排的真实输入 token 总和（AgentEventTracer 捕捉）。"""
    return self._last_input_tokens

@property
def last_output_tokens(self) -> int:
    """最近一轮编排的真实输出 token 总和（AgentEventTracer 捕捉）。"""
    return self._last_output_tokens
```

- **单 agent 路径**（`_run_single_agent_path`）：在 `finally: tracer.close()`（L831-832）中设置 `self._last_input_tokens = tracer.input_tokens`、`self._last_output_tokens = tracer.output_tokens`（成功与异常分支统一覆盖；异常时已执行的部分 token 也被记录）
- **编排路径**（run 方法）：
  - L1209-1210 重置处同步重置：`self._last_input_tokens = 0`、`self._last_output_tokens = 0`
  - L1222-1224 汇总处累加 TaskResult 的 token：

```python
self._last_input_tokens = sum(r.input_tokens for r in orchestrator._last_results)
self._last_output_tokens = sum(r.output_tokens for r in orchestrator._last_results)
```

### 4. chat_service.py：assistant 消息 tokens 用真实值

`_persist_conversation_history`（L261-266）assistant 消息 tokens 改为：

```python
# 真实 token 消耗：AgentEventTracer 捕捉的 input+output 之和；
# 为 0（如 policy_qa 自有 LLM 链路不经 tracer、异常降级）时回退文本估算
real_tokens = 0
if orchestrator_service is not None:
    try:
        real_tokens = (
            int(orchestrator_service.last_input_tokens or 0)
            + int(orchestrator_service.last_output_tokens or 0)
        )
    except Exception:
        real_tokens = 0
...
"tokens": real_tokens if real_tokens > 0 else _estimate_tokens(final_output),
```

- **user 消息 tokens 保持估算不变**（L243）：user 消息本身无 LLM 调用，input_tokens 已含完整 prompt（system + 历史 + 用户输入），本轮总消耗记在 assistant 消息上
- 中断路径（chat_service L751-757 finally aborted 落库）自动生效：读取同一属性，agent 已执行部分的真实 token 被记录
- int() 转换 + try/except 兜底：MagicMock 注入的测试环境（int(MagicMock) 抛 TypeError）自动回退估算，现有测试不被破坏

### 5. 测试

- **test_agent_event_tracer.py 新增用例**（复用现有 monkeypatch 事件类模式）：
  - `test_tokens_accumulated_without_langfuse`：`lf.enabled = False` 时 token 仍累积（解耦验证）
  - langfuse 启用时 token 不重复累加（on_event 一次 ModelCallEndEvent，summary 的 inputTokens 恰为输入值，无双重计数）
- **test_real_tokens_persist.py 新建**（复用 test_beijing_time_writes.py 的 `_persist_conversation_history` 测试模式）：
  - orchestrator_service（MagicMock）提供 `last_input_tokens=150` / `last_output_tokens=60` → assistant tokens == 210，user tokens 仍为估算
  - token 为 0 → 回退 `_estimate_tokens(final_output)`（与旧行为一致）
  - orchestrator_service=None → 回退估算
  - 属性为非数值（MagicMock 默认）→ int() 异常回退估算
  - TaskResult 新增字段默认 0（pydantic 模型字段存在性断言）
  - base.py `_run_single_agent` 源码静态断言（finally 中写入 result token，避免重型 agent mock）
  - orchestrator_service.py 汇总逻辑源码静态断言（sum(r.input_tokens ...) 存在）

## Assumptions & Decisions

1. **改造点 1 零改动**：分享接口已有 files/upload_files 且测试覆盖，本计划仅记录核查结论
2. **token 总和记在 assistant 消息**：user 消息保持估算——input_tokens 已包含完整 prompt，本轮 LLM 总消耗 = input + output 归属 assistant 消息语义最准确
3. **token 累积解耦 langfuse**：复用 `_collect_citations` 的"先于埋点执行"模式，langfuse 关闭时 token 也能捕捉（否则关闭 langfuse 后真实 token 恒为 0，改造失去意义）
4. **单 agent 路径在 finally 设置 token**：成功/异常统一覆盖，异常时部分已执行调用的 token 也不丢失
5. **编排路径累加所有 TaskResult**：多 agent（含 parallel）各 agent token 求和为本轮总消耗
6. **policy_qa / parallel 汇总 LLM 等不经 agentscope 事件流的调用不受覆盖**（其 token 不被 tracer 捕捉，回退估算）；用户要求明确限定"agent_event_tracer.py 中捕捉到的"，不扩展
7. **为 0 回退估算**：保证降级路径（未启用 langfuse 的存量会话、异常）落库行为不劣化

## Verification

1. `python -m py_compile app/utils/agent_event_tracer.py app/orchestrator/base.py app/services/orchestrator_service.py app/services/chat_service.py`
2. `python -m pytest tests/test_agent_event_tracer.py tests/test_real_tokens_persist.py tests/test_beijing_time_writes.py tests/test_upload_inject_and_bind.py tests/test_message_share.py -v` 全绿
3. `python -m pytest tests/ -q` 全量回归（已知 2 个 regulations dashboard 401 存量失败与本次无关）
4. 手动验证（可选）：langfuse 控制台对一轮对话查看 span summary 的 inputTokens/outputTokens 之和，与会话详情接口返回的 assistant 消息 tokens 字段一致；关闭 langfuse 后同样一致
