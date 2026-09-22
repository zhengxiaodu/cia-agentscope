# 计划：业务智能体 LLM 调用加 app_serial_number 请求头

## Summary
业务智能体（OpenAIChatModel 流式对话）的每次大模型调用加请求头 `app_serial_number: OIA-AGENTSCOPE-{message_pair_id}`。意图识别、改写、编排汇总走独立的 `AsyncOpenAI` 客户端（`intent_client`/`summary_client`/`think_client`，不经 OpenAIChatModel），**天然不受影响，无需排除处理**。

## Current State Analysis
调用链（唯一，已验证无其他分支）：
```
chat_service.generate_response (L553 生成 message_pair_id)
  → orchestrator_service.run(...)                 [L609，未传 message_pair_id]
  → _prepare_workspace_components()               [L367 传 create_model_fn=self._create_model_fn]
  → assemble_workspace_components(create_model_fn) [workspace_assembler.py L163，原样透传]
  → AgentRegistry(create_model_fn)                [registry.py L137: model = self._create_model_fn()]
  → create_model_from_config()                    [chat_service.py L429-460，创建 OpenAIChatModel]
```
- [chat_service.py:447-456](app/services/chat_service.py#L447-L456)：`OpenAIChatModel(credential, model, stream=True, parameters, context_size, extra_body)` — 尚无 client_kwargs
- `create_model_from_config` 仅被 `orchestrator_service._create_model_fn`（L158）调用
- `orchestrator_service.run()` 仅被 chat_service L609 调用（L793 是 run() 内部对编排器的调用，非本服务）
- agentscope 未装在沙箱，无法本地验证签名，但用户已确认 OpenAIChatModel 支持 `client_kwargs`（透传给底层 openai SDK 客户端构造，`default_headers` 为标准用法）

## Proposed Changes

### 1. app/services/chat_service.py — `create_model_from_config` 加参
```python
def create_model_from_config(model_config: dict, message_pair_id: str = ""):
```
构造 OpenAIChatModel 时，message_pair_id 非空则加：
```python
client_kwargs = (
    {"default_headers": {"app_serial_number": f"OIA-AGENTSCOPE-{message_pair_id}"}}
    if message_pair_id else None
)
model = OpenAIChatModel(
    ...,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    **({"client_kwargs": client_kwargs} if client_kwargs else {}),
)
```

### 2. app/services/chat_service.py — run() 调用处传 message_pair_id（L609-619）
```python
orchestrator_service.run(
    full_messages,
    ...,
    message_pair_id=message_pair_id,   # 新增
)
```

### 3. app/services/orchestrator_service.py — 三处
a. `run()` 签名加 `message_pair_id: Optional[str] = None`（docstring 补一行）
b. `run()` 内调用 `_prepare_workspace_components(...)` 时透传 `message_pair_id=message_pair_id`
c. `_prepare_workspace_components()` 签名加 `message_pair_id`；L367 改为闭包绑定当前请求的 pair_id：
```python
create_model_fn=lambda: self._create_model_fn(message_pair_id),
```
d. `_create_model_fn` 改签名 `(self, message_pair_id: Optional[str] = None)`，转调：
```python
return create_model_from_config(default_model_cfg, message_pair_id=message_pair_id or "")
```

**不改** registry.py / workspace_assembler.py（`create_model_fn` 仍是无参可调用，闭包已绑定值）。

## Assumptions & Decisions
- 请求头格式：`{"app_serial_number": "OIA-AGENTSCOPE-{message_pair_id}"}`
- 覆盖范围：单智能体直答 + 三种编排器（parallel/pipeline/react）的所有业务 agent LLM 调用（全部经 registry.create_agent → 工厂函数）
- 排除范围自动成立：意图识别/改写/编排（think/summary）用 AsyncOpenAI 客户端，与 OpenAIChatModel 创建路径完全隔离
- message_pair_id 为空（防御性默认）时不加头，保持旧行为
- `client_kwargs={"default_headers": {...}}` 为 openai SDK 标准构造参数；若 agentscope 版本字段名不同，上线时以一次真实调用验证（见 Verification）

## Verification（按用户要求最小化）
1. `python -m py_compile app/services/chat_service.py app/services/orchestrator_service.py`
2. 不跑 pytest
3. 提醒用户：沙箱无 agentscope，无法验证 client_kwargs 实际字段名；建议部署后抓一次请求（或网关日志）确认头已带上
