# 分享详情文件字段核查 + 真实 token 消耗持久化 改造计划（简化版）

## Summary

两个改造点：

1. **分享详情接口文件字段核查**：经核实 `GET /message_share/{shared_id}` **已经返回** `files`（系统产出文件）与 `upload_files`（用户上传文件）两个列表并按 message_pair_id 过滤，**无需代码改动**（仅记录核查结论）
2. **真实 token 消耗持久化（简化方案）**：在 `orchestrator_service` 的事件流转发处直接拦截 `MODEL_CALL_END` 事件解析 token（用户建议的方案），累加后经 `last_input_tokens` / `last_output_tokens` 属性传给 chat_service，替代 `_estimate_tokens` 估算。**只改 2 个文件**，不动 agent_event_tracer.py 和 base.py

## Current State Analysis

### 改造点 1：分享详情已有文件字段（核查结论，无改动）

- [message_share.py L100-107](file:///workspace/app/routes/message_share.py#L100-L107)：detail 的 `files` / `upload_files` **已经**按 `message_pair_id ∈ 分享 pair 集合` 过滤并返回
- SessionDetailResponse（[session.py L53-L60](file:///workspace/app/models/session.py#L53-L60)）本身含这两个列表字段
- test_message_share.py `test_get_share_no_auth_required_and_filters` 已断言 files/upload_files 过滤行为

### 改造点 2：token 估算 → 真实值（简化拦截方案）

关键链路事实（已核实）：

- **事件流汇聚点**：[orchestrator_service.py `_run_with_ws_task`](file:///workspace/app/services/orchestrator_service.py#L1060) 内两处 `async for ev in ...: yield ev` 是所有 agent 事件的必经转发点——单 agent 路径（L1084 `_run_single_agent_path`）+ 编排路径（L1211 `orchestrator.run`，覆盖 parallel/pipeline/sequential 所有模式的全部 agent）。**单协程顺序消费，无并发问题**（与 L1209-1224 读写 `_last_agent_ids` 同模式；L478 的并发禁令只针对 `create_task` 并行执行的 `_resolve_workspace_task`）
- **流经的是 SSE 字符串**（`"data: {...}\n\n"`），但 `ModelCallEndEvent` 序列化格式已实测确认：`{"id":"...","type":"MODEL_CALL_END","reply_id":"r1","input_tokens":100,"output_tokens":40,...}`——`type` / `input_tokens` / `output_tokens` 字段可直接解析
- **[chat_service.py L266](file:///workspace/app/services/chat_service.py#L266)**：assistant 消息 `tokens` 目前用 `_estimate_tokens(final_output)` 估算
- **属性传递先例**：`last_agent_ids` / `last_success`（`__init__` L141-143 + property L232-249），chat_service L249-260 已在读，token 属性照抄该模式
- **SSE 拦截的容错先例**：`"MODEL_CALL_END" in ev` 快速字符串判断（命中才 json.loads，几乎零开销）；误命中（文本内容含该串）由后续 `type ==` 校验排除
- **agent_event_tracer 的门控问题自动绕开**：tracer 的 token 累积受 langfuse 启用门控（`on_event` → `_enabled` → `_dispatch`），而 SSE 流拦截独立于 tracer，langfuse 关闭时事件流依然存在

## Proposed Changes

### 1. orchestrator_service.py：事件流拦截 + 属性暴露

**(a) `__init__`（L141-143 附近）新增状态**：

```python
# 最近一轮 agent 事件流捕捉的真实 token 用量（MODEL_CALL_END 累积）
self._last_input_tokens: int = 0
self._last_output_tokens: int = 0
```

**(b) property 区（L232-249 附近，紧随 last_success）新增**：

```python
@property
def last_input_tokens(self) -> int:
    """最近一轮编排的真实输入 token 总和（MODEL_CALL_END 事件累积）。"""
    return self._last_input_tokens

@property
def last_output_tokens(self) -> int:
    """最近一轮编排的真实输出 token 总和（MODEL_CALL_END 事件累积）。"""
    return self._last_output_tokens
```

**(c) 新增私有异步生成器 helper**（放在 `_run_with_ws_task` 附近）：

```python
async def _capture_tokens_from_stream(self, source: AsyncGenerator[str, None]):
    """转发 SSE 事件流，旁路解析 MODEL_CALL_END 事件累积真实 token。

    agentscope 的事件以 "data: {...}\\n\\n" 字符串流转经本方法；先做
    子串快速判断（避免每条事件都 json.loads），命中后解析并校验 type，
    再累加 input_tokens / output_tokens。解析失败静默容错，不影响转发。
    """
    async for ev in source:
        try:
            if "MODEL_CALL_END" in ev:
                payload = json.loads(ev.removeprefix("data: ").strip())
                if payload.get("type") == "MODEL_CALL_END":
                    self._last_input_tokens += int(payload.get("input_tokens") or 0)
                    self._last_output_tokens += int(payload.get("output_tokens") or 0)
        except Exception:
            logger.debug("[OrchestratorService] token 拦截解析失败", exc_info=True)
        yield ev
```

**(d) 接入两处转发点**：

- `_run_with_ws_task` 开头（L1073 单 agent 短路路径之前，docstring 之后）重置：`self._last_input_tokens = 0`、`self._last_output_tokens = 0`（两条路径公共入口，一次重置覆盖全部）
- L1084 单 agent 路径：`async for ev in self._capture_tokens_from_stream(self._run_single_agent_path(...)): yield ev`
- L1211 编排路径：`async for ev in self._capture_tokens_from_stream(orchestrator.run(...)): yield ev`

### 2. chat_service.py：assistant 消息 tokens 用真实值

`_persist_conversation_history`（L261-266）assistant 消息 tokens 改为：

```python
# 真实 token 消耗：MODEL_CALL_END 事件累积的 input+output 之和；
# 为 0（如 policy_qa 自有 LLM 链路、异常降级）时回退文本估算
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

- **user 消息 tokens 保持估算不变**（L243）：input_tokens 已含完整 prompt，本轮 LLM 总消耗记在 assistant 消息上
- 中断路径（chat_service L751-757 finally aborted 落库）自动生效
- int() + try/except 兜底：MagicMock 测试环境（int(MagicMock) 抛 TypeError）自动回退估算，现有测试不被破坏

### 3. 测试（新建 tests/test_orchestrator_token_capture.py）

复用现有模式（真实 `ModelCallEndEvent` 构造 + MagicMock service + `_persist_conversation_history` 直调）：

**helper 拦截逻辑**（用真实 agentscope 事件类构造 SSE 字符串，不手写 JSON）：
- 含 2 个 MODEL_CALL_END 的流 → `last_input_tokens` / `last_output_tokens` 正确累加，且事件原样转发（数量与内容不变）
- 其他事件（TextBlockDelta / 自定义 `self._event` 字符串）透传不计数
- 文本内容中包含 "MODEL_CALL_END" 字样的非模型事件 → 子串命中但 type 校验排除，不计数
- JSON 解析异常（畸形字符串）→ 容错不抛、照常转发
- 重置验证：先累积，再跑一遍重置路径后归零（构造 OrchestratorService 实例走 `_run_with_ws_task` 源码断言重置存在，或直接单测属性重置）

**chat_service 落库**：
- mock orchestrator_service 提供 `last_input_tokens=150` / `last_output_tokens=60` → assistant tokens == 210，user tokens 仍为估算
- token 为 0 → 回退 `_estimate_tokens(final_output)`
- orchestrator_service=None → 回退估算
- 属性为非数值（MagicMock 默认）→ int() 异常回退估算

**接入静态断言**：orchestrator_service.py 源码中两处转发循环均经 `_capture_tokens_from_stream` 包装（grep 断言，避免重型集成测试）

## Assumptions & Decisions

1. **改造点 1 零改动**：分享接口已有 files/upload_files 且测试覆盖
2. **采用用户建议的 SSE 流拦截方案**（相对原 TaskResult 方案少改 2 个文件）：不动 agent_event_tracer.py（无需解耦 langfuse 门控）、不动 base.py（无需 TaskResult 加字段）；单点拦截同时覆盖单 agent + 编排所有模式
3. **子串快速判断 + type 校验双层过滤**：`"MODEL_CALL_END" in ev` 避免每条事件 json.loads（事件流高频，性能几乎零开销）；误命中由 `payload.get("type") == "MODEL_CALL_END"` 排除
4. **token 总和记在 assistant 消息**，user 消息保持估算（input_tokens 已含完整 prompt）
5. **意图识别 / 查询改写 / parallel 汇总 / policy_qa 自有 LLM 不经事件流，不被捕捉**（回退估算）——用户要求限定"事件流中的 tokens"，不扩展
6. **helper 内异常静默容错**：token 统计是旁路功能，绝不影响事件流转发主流程
7. **重置点在 `_run_with_ws_task` 开头**：两条路径公共入口一次重置，避免上一轮残留

## Verification

1. `python -m py_compile app/services/orchestrator_service.py app/services/chat_service.py`
2. `python -m pytest tests/test_orchestrator_token_capture.py tests/test_beijing_time_writes.py tests/test_upload_inject_and_bind.py tests/test_message_share.py -v` 全绿
3. `python -m pytest tests/ -q` 全量回归（已知 2 个 regulations dashboard 401 存量失败与本次无关）
4. 手动验证（可选）：跑一轮对话，对比 langfuse span summary 的 inputTokens+outputTokens 与会话详情接口 assistant 消息的 tokens 字段一致；关闭 langfuse 后同样一致
