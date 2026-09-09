# 并行编排结束后新增 LLM 结果汇总

## Summary

并行编排（ParallelOrchestrator）多智能体执行结束后的 `summary` 事件目前只是机械拼接（`已为您完成 N 项任务：\n{输出1}\n\n---\n\n{输出2}`，见 [parallel.py:141-152](file:///workspace/app/orchestrator/parallel.py#L141-L152)），没有 LLM 参与整合。本次改为：当并行执行出 **多于 1 个** 任务结果时，调用一次轻量 LLM（`chat_complete` 非流式调用，**默认业务大模型 models.default**）将各任务结果整合为一份连贯的最终回答；LLM 失败/超时/关闭时回退现有拼接格式。单结果、全失败场景行为不变。

## Current State Analysis

- `ParallelOrchestrator.run()`：asyncio.Queue 汇聚各 agent 事件 → 收集 `TaskResult`（成功与失败任务的 output 都会进 `summary_parts`）→ 末尾按 `len(summary_parts)` 分支发 `summary` 事件
- `summary` 事件内容 = chat_service 的 `final_output`（[chat_service.py:612-613](file:///workspace/app/services/chat_service.py#L612-L613)）→ 落库 assistant 消息、输出敏感检测、推荐问题依据，因此 summary 质量直接影响最终答案
- `ParallelOrchestrator.__init__(agent_factory, timeout)` 无任何 LLM 客户端；而 `ReActOrchestrator` 已有注入 `think_client`/`think_model_config` 的成熟模式（[orchestrator_service.py:165-184](file:///workspace/app/services/orchestrator_service.py#L165-L184)）
- `chat_complete`（[llm_client.py:29](file:///workspace/app/intent/llm_client.py#L29)）：非流式调用，自带 Langfuse generation 埋点（OTel 上下文自动挂在当前活跃 span 下），`enable_thinking: False`
- `orchestrator_params` 从 `config/intent_config.yml` 的 `orchestrator` 段在 `OrchestratorService.create` 读一次（[orchestrator_service.py:140-146](file:///workspace/app/services/orchestrator_service.py#L140-L146)）
- 既有测试 `tests/test_degraded_and_parallel.py` 以 `ParallelOrchestrator(agent_factory=MagicMock(), timeout=5.0)` 构造（新参数须保持可选向后兼容）
- TTFT 判定（`_compute_ttft_marker`）只认 `TEXT_BLOCK_DELTA`/`summary`，新增其他事件类型不影响 TTFT

## Proposed Changes

### 1. `app/orchestrator/parallel.py`（核心改动）

构造函数追加 4 个可选参数（全部有默认值，保证既有调用/测试不破）：

```python
def __init__(
    self,
    agent_factory: AgentFactory,
    timeout: float = 60.0,
    summary_client: Optional[AsyncOpenAI] = None,
    summary_model_config: Optional[dict] = None,
    summary_enabled: bool = True,
    summary_timeout: float = 60.0,
):
```

`run()` 末尾汇总段重写（③ 汇总事件）：

```python
# ③ 汇总事件
if len(summary_parts) > 1:
    summary = await self._summarize(intent_result, self._last_results, langfuse_service)
    if summary:
        yield self._event({"type": "summary", "content": summary})
    else:  # 回退：机械拼接
        summary = "\n\n---\n\n".join(summary_parts)
        yield self._event({
            "type": "summary",
            "content": f"已为您完成 {len(summary_parts)} 项任务：\n{summary}",
        })
elif summary_parts:
    yield self._event({"type": "summary", "content": summary_parts[0]})
```

新增私有方法 `_summarize`：

- 触发条件：`summary_enabled` 且 `summary_client`/`summary_model_config` 非空 且 结果数 > 1（调用方已保证）
- 调用前 yield 一个 additive 提示事件 `{"type": "parallel_summary", "status": "started", "message": "正在汇总各任务结果..."}`（前端可忽略未知类型；不进 TTFT 判定，无契约破坏）——由于 `run` 是生成器且 `_summarize` 需要发事件，把事件 yield 逻辑留在 `run` 内：`_summarize` 仅返回 `Optional[str]`，started 事件在 `run` 中调用前 yield
- prompt 构造（模块级常量）：
  - system：`_SUMMARY_SYSTEM_PROMPT` —— 多任务汇总助手角色；要求按主题整合而非罗列、保留关键信息与数据、失败任务如实简要说明、不编造、无寒暄
  - user：用户问题（`intent_result.rewritten_query`）+ 逐个任务结果块 `【任务N - {intent_id}（智能体：{agent_id}）- 成功/失败】\n{output}`；单个 output 超过 `_MAX_TASK_OUTPUT_CHARS = 4000` 字符时截断并追加 `...（内容过长已截断）`
- 调用：`async with asyncio.timeout(self._summary_timeout):` 包裹 `await chat_complete(self._summary_client, self._summary_model_config, system, user, stage=str(TraceName.LLM_PARALLEL_SUMMARY))`
- 返回 LLM 文本（strip 后非空）；`asyncio.TimeoutError`/任何异常记 warning 返回 `None`（调用方回退拼接）
- import 变更：`from openai import AsyncOpenAI`、`from app.intent.llm_client import chat_complete`、`from app.utils.trace_names import TraceName`（trace_names 按现有 react.py 的用法传 str）

### 2. `app/services/orchestrator_service.py`（接线）

- `create()` 中（已有 `default_model_cfg`）：新增 `summary_client = create_async_client(default_model_cfg)`，随构造传入存为 `self._summary_client`、`self._summary_model_cfg = default_model_cfg`（构造函数 `__init__` 加两个参数）
- `orchestrator_params` 字典新增两个键：
  - `"parallel_summary_enabled": orchestrator_cfg.get("parallel_summary", True)`
  - `"parallel_summary_timeout": orchestrator_cfg.get("parallel_summary_timeout", 60)`
- `_create_orchestrator` 的 parallel 分支传入：

```python
return ParallelOrchestrator(
    agent_factory=agent_factory,
    timeout=self._orchestrator_params["parallel_timeout"],
    summary_client=self._summary_client,
    summary_model_config=self._summary_model_cfg,
    summary_enabled=self._orchestrator_params["parallel_summary_enabled"],
    summary_timeout=self._orchestrator_params["parallel_summary_timeout"],
)
```

### 3. `config/intent_config.yml`

`orchestrator` 段追加：

```yaml
  # 并行执行结束后的 LLM 结果汇总开关（false 或失败时回退简单拼接）
  parallel_summary: true
  # 汇总 LLM 调用超时（秒），超时回退拼接
  parallel_summary_timeout: 60
```

### 4. `app/utils/trace_names.py`

LLM 段新增一行（保持命名约定）：

```python
    LLM_PARALLEL_SUMMARY = "llm-parallel-summary"
```

### 5. 测试

新增 `tests/test_parallel_summary.py`（monkeypatch `app.orchestrator.parallel.chat_complete`，mock `_run_single_agent` 产出 TaskResult）：

- 多结果 + LLM 成功：summary 事件 content 为 LLM 文本；chat_complete 被调用且 prompt 含两个任务输出与 rewritten_query；先收到 `parallel_summary started` 事件
- 多结果 + LLM 抛异常：回退拼接（`已为您完成 2 项任务`），无未捕获异常
- 多结果 + LLM 超时（mock sleep，`summary_timeout=0.05`）：回退拼接
- `summary_enabled=False` / `summary_client=None`：不调用 LLM，直接拼接
- 单结果：不调用 LLM，summary = 该输出（现行为）
- 失败任务（success=False, output="执行超时"）：prompt 中带 `- 失败` 标记（成功为 `- 成功`）
- 超长输出截断：构造 5000 字符 output，prompt 中含截断标记

检查 `tests/test_degraded_and_parallel.py`：构造签名向后兼容（新参数全可选），预期无需修改；跑一遍确认。

## Assumptions & Decisions

- **轻量 LLM 调用而非真实智能体**（用户已确认）：不创建 agent，避免工具循环、状态污染与额外延迟；总结模型用 `models.default`（用户已确认），`enable_thinking` 已由 `chat_complete` 关闭
- **只在结果数 > 1 时总结**：单结果直接透传（与现有分支一致，单意图本就走 pipeline，parallel 单结果多为边界情形）
- **失败任务如实进入总结 prompt**（标记 成功/失败），与项目"诚实上报"风格一致；回退路径保持现状拼接（失败任务的错误文案本就在 summary_parts 中）
- **summary 事件契约不变**：仍是一个 `{"type":"summary","content":...}` 事件，chat_service/前端零改动；`parallel_summary started` 为 additive 事件，未知类型可被忽略
- **不新增编排级 span**：`chat_complete` 自带的 generation 埋点（挂在 chat-response 根 span 下）已可观测；pipeline 的编排级 span 属既有差异，不在本次范围
- Langfuse 未启用/模型配置缺失时调用失败自然走回退，无需专门开关

## Verification

1. `python -m py_compile app/orchestrator/parallel.py app/services/orchestrator_service.py app/utils/trace_names.py`
2. `python -m pytest tests/test_parallel_summary.py tests/test_degraded_and_parallel.py -q` 全部通过
3. 全量回归 `python -m pytest tests/ -q` 通过
4. 人工核对：`parallel_summary: false` 配置路径与 LLM 异常路径都产出回退拼接文案
