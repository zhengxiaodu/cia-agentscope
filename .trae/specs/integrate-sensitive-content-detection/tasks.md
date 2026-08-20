# Tasks

- [ ] Task 1: 新增安全敏感服务配置
  - [ ] SubTask 1.1: 在 `.env` 中新增 `SENSITIVE_SERVICE_URL=http://25.59.38.152:30017/sensitive/check`、`SENSITIVE_THRESHOLD=0.7`、`SENSITIVE_TIMEOUT=5`
  - [ ] SubTask 1.2: 在 `app/config.py` 中新增 `SENSITIVE_SERVICE_URL = os.getenv("SENSITIVE_SERVICE_URL", "")`、`SENSITIVE_THRESHOLD = float(os.getenv("SENSITIVE_THRESHOLD", "0.7"))`、`SENSITIVE_TIMEOUT = float(os.getenv("SENSITIVE_TIMEOUT", "5"))`

- [ ] Task 2: 新建敏感检测服务封装 `app/services/sensitive_service.py`
  - [ ] SubTask 2.1: 实现 `async def check_sensitive(text: str, stage: str = "input") -> dict`，返回 `{"blocked": bool, "reason": str, "category": str, "raw": dict}`
  - [ ] SubTask 2.2: 使用 `httpx.AsyncClient(timeout=SENSITIVE_TIMEOUT)` 调用敏感检测服务，POST JSON `{"text": text, "threshold": SENSITIVE_THRESHOLD}`
  - [ ] SubTask 2.3: 解析响应：`code==0 and hasSensitiveWord==True` 时 `blocked=True`，`reason` 取 `semantic.reason`，`category` 取 `semantic.category`
  - [ ] SubTask 2.4: 兜底逻辑：`SENSITIVE_SERVICE_URL` 为空 / 网络异常 / 超时 / HTTP 非 2xx / JSON 解析失败 / `code!=0` 时，记录 warning 日志，返回 `blocked=False` 放行
  - [ ] SubTask 2.5: Langfuse 埋点：使用 `get_current_langfuse()` 获取单例，用 `start_span("sensitive-check", input={"text": text, "threshold": ..., "stage": stage})` 包裹 HTTP 调用，span 退出前 `update(output={"blocked":..., "reason":..., "category":..., "raw":...}, metadata={"latency_ms":..., "stage":stage})`

- [ ] Task 3: 在 `/chat` 接口接入用户输入检测
  - [ ] SubTask 3.1: 在 `app/routes/chat.py` 的 `stream()` 内、`session_ready` 事件之后、`generate_response` 之前，提取 `body.messages` 最后一条 user 消息文本
  - [ ] SubTask 3.2: 调用 `check_sensitive(user_input, stage="input")`
  - [ ] SubTask 3.3: 若 `blocked=True`，yield `message_replace` 事件（`stage="input"`，`reason` 携带风险原因），然后 `return` 结束流（不进入 generate_response）
  - [ ] SubTask 3.4: 若 `blocked=False`，正常进入 `generate_response`

- [ ] Task 4: 在 `generate_response` 接入 final_output 检测
  - [ ] SubTask 4.1: 在 `app/services/chat_service.py` 的 `generate_response` 中，编排流结束后、计算 `final_output` 完毕、推荐问题生成之前，调用 `check_sensitive(final_output, stage="output")`
  - [ ] SubTask 4.2: 若 `blocked=True`，yield `message_replace` 事件（`stage="output"`，`reason` 携带风险原因），跳过 `_emit_recommended_questions`
  - [ ] SubTask 4.3: 若 `blocked=False`，正常执行推荐问题生成等后续步骤
  - [ ] SubTask 4.4: 命中敏感时，持久化与文件检测步骤仍执行（保证会话数据完整），仅跳过推荐问题

- [ ] Task 5: 编写单元测试
  - [ ] SubTask 5.1: 测试 `check_sensitive` 命中场景（mock httpx 返回 `code=0, hasSensitiveWord=true`）
  - [ ] SubTask 5.2: 测试 `check_sensitive` 未命中场景（mock httpx 返回 `code=0, hasSensitiveWord=false`）
  - [ ] SubTask 5.3: 测试兜底场景：`SENSITIVE_SERVICE_URL` 为空、超时、HTTP 500、`code!=0` 均返回 `blocked=False`
  - [ ] SubTask 5.4: 测试 `chat.py` 输入命中时不进入 `generate_response` 且发送 `message_replace` 事件

# Task Dependencies
- Task 3 依赖 Task 1、Task 2
- Task 4 依赖 Task 1、Task 2
- Task 5 依赖 Task 2、Task 3、Task 4
- Task 3 与 Task 4 可并行
