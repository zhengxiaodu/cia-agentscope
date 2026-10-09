# 敏感检测 Langfuse trace 并入会话 trace 实施计划

## 概要

用户输入的敏感检测（`dict_check_sensitive`）目前在 `routes/chat.py` 中、`generate_response` 之前调用——此时会话根 span（"chat-response"）尚未创建，`start_as_current_observation` 找不到父级 observation，导致 sensitive-check 形成独立 trace 和独立 trace_id。

修复：把输入检测移入 `generate_response` 的根 span 内部执行，使 `sensitive-dict-check` span 嵌套在本轮会话 trace 中。

## 现状分析（Phase 1 探索结论）

- **根因链路**：
  - [routes/chat.py](file:///workspace/app/routes/chat.py#L53-L62)：`stream()` 中先调 `dict_check_sensitive(user_input, stage="input")`，命中即 yield `message_replace` 并 return，**之后**才调用 `generate_response`
  - [chat_service.py](file:///workspace/app/services/chat_service.py#L488-L515)：根 span "chat-response" 在 `generate_response` 内 ③ 处创建
  - [langfuse_service.py](file:///workspace/app/services/langfuse_service.py#L88-L114)：`start_span` 用 `start_as_current_observation`（OTel context propagation），无活跃父 span 时自动成为新 trace 的根
- **输出检测无此问题**：`check_sensitive(stage="output")` 在 L577，位于 `with root_span_ctx` 块内，正确嵌套
- **其他事实**：
  - [config.py](file:///workspace/app/config.py#L4) `load_dotenv()` 在 import 时执行 → 测试环境中 `SENSITIVE_SERVICE_URL` 是真实地址，任何未 mock 的真实调用会发真实 HTTP
  - `generate_response` 生产环境调用方仅 routes/chat.py（/workspace/test.py 为遗留草稿）
  - 当前输入被拦截时：不创建 trace、不持久化消息（保持该行为）
  - `file_parse_service` 的 upload-parse span 是上传流程（无会话上下文），独立 trace 合理，不在本次范围

## 具体改动

### 1. [app/routes/chat.py](file:///workspace/app/routes/chat.py)

- 删除 `stream()` 中的输入敏感检测块（L53-62：`_extract_user_input` + `dict_check_sensitive` + 命中处理）
- 清理不再使用的导入：`_extract_user_input`（L11）、`build_message_replace_event` / `dict_check_sensitive`（L12-15）
- `session_ready` 事件后直接进入 `generate_response`

### 2. [app/services/chat_service.py](file:///workspace/app/services/chat_service.py)

**a) 导入**（L26-29 区域）：在现有 `from app.services.sensitive_service import ...` 中追加 `dict_check_sensitive`。

**b) `generate_response` 根 span 内新增输入检测**——位置：`with root_span_ctx as root_obs:` 块内、trace_ready 下发之后、④ 编排执行之前：

```python
# ③.5 用户输入敏感检测（纯词典，低延迟）。
# 置于根 span 内执行，使 sensitive-dict-check span 嵌套在本轮会话
# trace 中（而非形成独立 trace）；命中则发 message_replace 并结束本轮，
# 不进入编排主流程，也不持久化消息
sens_input = await dict_check_sensitive(
    _extract_user_input(messages), stage="input"
)
if sens_input["blocked"]:
    replace_event = build_message_replace_event(sens_input, stage="input")
    yield f"data: {json.dumps(replace_event, ensure_ascii=False)}\n\n"
    _safe_update_span(root_obs, {
        "reply": "",
        "session_id": session_id,
        "user_id": user_id,
        "sensitiveBlocked": True,
        "blockedStage": "input",
    })
    return
```

- 早退路径：`with` 正常退出、`finally` 中 `_finalize_trace` 照常 flush trace；`aborted=False` 故不持久化——与现行为一致
- 顺序影响：检测前多了①历史加载与②目录快照（均为快速本地/DB操作），可接受

**c) docstring 更新**（L451-459）：敏感拦截描述改为"输入检测在本函数根 span 内（trace 嵌套），输出检测在编排后"。

### 3. [tests/test_sensitive_service.py](file:///workspace/tests/test_sensitive_service.py)

**a) `_build_request`**：补 `request.app.state.langfuse_service = None`、`request.app.state.orchestrator_service = MagicMock()`（供新测试直穿 route → generate_response）。

**b) 重写 chat 入口三个测试**（原 patch `chat_route.dict_check_sensitive` 已失效）：
- `test_chat_input_blocked_emits_message_replace`：patch `chat_service_module.dict_check_sensitive` 返回命中 → 走真实 `generate_response`；断言事件序列 `["session_ready", "message_replace"]`、orchestrator.run 未被调用
- `test_chat_input_safe_calls_generate_response`：patch 返回放行 + orchestrator mock（run yield summary）→ 断言 run 被调用、事件含 summary
- `test_chat_input_fallback_continues`：patch 返回兜底放行 → 主流程正常（同上结构）

  三个测试均需 `session_service.load_messages = AsyncMock(return_value=[])`；orchestrator.run 为 yield summary 的 async gen mock。

**c) 既有 generate_response 级测试补丁**（`test_generate_response_output_blocked` / `test_generate_response_output_safe`）：新增 patch `chat_service_module.dict_check_sensitive`（安全放行 fake），避免真实 HTTP（.env 已加载真实服务地址）。

## 假设与决策

1. 输入被拦截时本轮仍会创建会话 trace（含 message_replace 结果）——比原行为（完全无 trace）更可观测，符合"包含在每一次会话中"的要求。
2. 输入检测从"历史加载/快照之前"移到"之后"，延迟差异为一次 DB 查询 + 目录快照，可接受；换来 trace 正确嵌套。
3. `upload-parse`（上传后台解析）独立 trace 保持不变——上传时无会话上下文，属另一流程。
4. `test.py`（根目录遗留草稿文件）不动。

## 验证步骤

1. `python -m pytest tests/test_sensitive_service.py -q` 全绿
2. 全量回归 `python -m pytest tests/ -q`（当前基线 168 passed）
3. grep 确认 routes/chat.py 无 `dict_check_sensitive` / `_extract_user_input` 残留导入
4. 人工核验逻辑：输入检测现位于根 span 内（代码审查确认 `with root_span_ctx` 块内调用），输出检测位置不变
