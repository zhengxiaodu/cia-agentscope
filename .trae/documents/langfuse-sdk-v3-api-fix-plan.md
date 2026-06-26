# Langfuse SDK V3+ API 适配修复 Plan

## 问题摘要

当前的 `LangfuseService` 使用 `self._client.trace()` 方法创建 trace，但 Langfuse V3+（当前安装的最新 SDK）构建在 OpenTelemetry 之上，**不存在 `trace()` 顶层方法**。V3+ 使用 `start_observation()` / `start_as_current_observation()` 等观察（Observation）为中心的 API。

## 当前状态分析

### 现有代码

1. **`app/services/langfuse_service.py`** — `create_trace()` 调用 `self._client.trace()` → `'Langfuse' object has no attribute 'trace'`
2. **`app/services/chat_service.py`** — `generate_response` 调用 `langfuse_service.create_trace()`, `langfuse_service.update_trace()`, `langfuse_service.flush()` 并读取 `trace.id`
3. **`app/services/langfuse_service.py`** 当前方法签名：
   - `create_trace(name, session_id, user_id, input)` → 返回 trace
   - `update_trace(trace, output, input)` → 无返回值
   - `flush()` → 无返回值

### Langfuse V3+/V4 SDK 关键 API（after research）

| V3+ API | 说明 |
|---|---|
| `Langfuse(public_key, secret_key, host)` | 客户端初始化（不变） |
| `client.start_observation(name, as_type, input, **kwargs) -> Observation` | 手动创建 observation，需调用 `.end()`。适合 generator 模式 |
| `client.start_as_current_observation(name, as_type, input, **kwargs) -> ContextManager[Observation]` | context manager，自动 `.end()`。不适合 generator |
| `Observation.update(input, output, ...)` | 更新 observation 属性 |
| `Observation.end()` | 结束 observation |
| `Observation.trace_id` | 获取 trace ID（32 字符 hex） |
| `client.flush()` | 同步刷新（不变） |
| `client.get_current_trace_id()` | 获取当前 trace ID（需 OTel 上下文） |

### Langfuse V3+ Manual Observation 的特性

- `start_observation(name, as_type, input=..., output=...)` — 直接创建，**不**设置 OTel context
- 返回的 `Observation` 对象有 `.update()`, `.end()`, `.trace_id` 属性
- `session_id`/`user_id` 等 trace-level 属性通过 OTel context 传播（context manager / `@observe()` decorator）。在 manual API 下，它们无法自动设置

**决议**：manual `start_observation()` 是 generator 场景下唯一可行方案（`@observe()` 和 context manager 均与 `yield` 不兼容）。`session_id` / `user_id` 将被包含在 `input`  dict 中作为上下文记录。这满足"追踪信息中包含输入输出"的需求，Langfuse 侧仍可通过 dashboard 查看完整输入。

## Proposed Changes

### 1. 修改 `app/services/langfuse_service.py`

**What**: 用 V3+ SDK 的 `start_observation()` / `Observation.end()` API 替换消失的 `trace()` 方法。

**Why**: 原 `create_trace()` / `update_trace()` 使用的 API 在 V3+ 中不存在，导致 `Langfuse` object has no attribute 'trace' 错误。

**How**:

- 删除 `create_trace()` 方法
- 删除 `update_trace()` 方法  
- 新增 `start_observation(name, as_type, input, ...)` → 返回 `Observation` 或 `None`
- 新增 `end_observation(observation, output)` → 调用 `observation.update(output=...)` + `observation.end()`
- 保留 `flush()` 不变
- 所有方法仍在 try/except 包裹，`_enabled` 逻辑不变

新方法签名：

```python
def start_observation(
    self,
    name: str = "chat-response",
    as_type: str = "span",
    input: Any = None,
) -> Optional[Any]:
    if not self._enabled or not self._client:
        return None
    try:
        return self._client.start_observation(
            name=name,
            as_type=as_type,
            input=input,
        )
    except Exception as e:
        logger.warning("Langfuse start_observation failed: %s", e)
        return None

def end_observation(
    self,
    observation,
    output: Any = None,
) -> None:
    if not self._enabled or not observation:
        return
    try:
        observation.update(output=output)
        observation.end()
    except Exception as e:
        logger.warning("Langfuse end_observation failed: %s", e)

def flush(self) -> None:
    # 不变
```

### 2. 修改 `app/services/chat_service.py`

**What**: 适配新的 `start_observation()` / `end_observation()` API。

**Why**: 调用方需改用新的 observation 创建/结束方法。

**How**:

- 将 `trace = langfuse_service.create_trace(session_id=..., user_id=..., input=...)` 替换为
  ```python
  obs = langfuse_service.start_observation(
      name="chat-response",
      as_type="span",
      input={"messages": messages, "session_id": session_id, "user_id": user_id},
  )
  ```
- 将 `langfuse_service.update_trace(trace, output=trace_output)` + `langfuse_service.flush()` 替换为
  ```python
  if obs:
      langfuse_service.end_observation(obs, output=...)
      langfuse_service.flush()
  ```
- 将 `trace_id = str(trace.id) if trace.id else None` 替换为
  ```python
  trace_id = obs.trace_id if obs else None
  ```
- 保留 `tool_calls` / `token_usage` 的 `trace_output` 构建逻辑不变
- 保留异常保护（try/except）不变

### 3. 不需要修改 `app/routes/chat.py` 和 `app/main.py`

`chat.py` 只负责传递 `langfuse_service` 对象，不关心其内部 API 变化。`main.py` 只负责初始化 `LangfuseService`，不变。

## Assumptions & Decisions

1. **Manual `start_observation()` 是最合适的方式** — 因为 `generate_response` 是 async generator（使用了 `yield`），context manager 和 `@observe()` decorator 都不兼容。Manual `start_observation()` 允许在 generator 的生命周期内任意位置调用 `.update()` 和 `.end()`。

2. **`session_id`/`user_id` 通过 input dict 记录** — 而非作为 trace-level  attribute 传播。这是因为 trace-level attributes 需要通过 OTel context (`propagate_attributes()` context manager) 设置，与 generator 模式不兼容。这些信息仍会完整出现在 Langfuse  dashboard 的 input 字段中。

3. **`as_type="span"`** — 使用通用 span 类型，不限定 "generation"/"tool"/"agent"。这是最安全的类型，Langfuse 会自动将其识别为 trace 的根 observation。

4. **返回 `trace_id` 的行为不变** — `observation.trace_id` 返回 Langfuse 使用的 trace ID，前端仍可通过 `TRACE_READY` SSE 事件获取。

5. **`_enabled` / 非强依赖逻辑完全不变** — 所有异常仍被单独 try/except 包裹。

## Verification Steps

1. 启动应用，确保 `Langfuse('Langfuse'...)` 初始化正确（无 `trace()` 错误）
2. 调用 `/chat` 产生一轮对话，检查 SSE 流末尾 `TRACE_READY` 事件中 `trace_id` 不为 null
3. 登录 Langfuse Dashboard (https://us.cloud.langfuse.com/)，确认 trace 出现在项目中，input/output 数据完整
4. 清理 `.env` 中 Langfuse 配置项后重启，确认 `/chat` 正常工作，`TRACE_READY` 中 `trace_id` 为 null
5. Python 语法检查：`py_compile` 所有修改文件