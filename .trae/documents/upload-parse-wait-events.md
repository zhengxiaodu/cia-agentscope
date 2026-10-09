# 上传文件解析等待（轮询 + 工具事件流 + 超时提示）改造计划

## Summary

当提问发生在上传文件解析完成之前：先向前端发送一对 `TOOL_CALL_START` / `TOOL_CALL_END` 事件（tool_call_name 为"等待mineru文件解析完成"，id 随机造），同时轮询等待解析完成（总超时 15 秒，代码常量，不进 .env）。等待成功则解析内容照常注入提示词；超时后仍在解析中的文件，在提示词中注入"解析失败"提示告知智能体，问答继续不阻塞。

## Current State Analysis

现状链路（均已探明）：

1. **状态机**（[app/dao/upload_file_dao.py](file:///workspace/app/dao/upload_file_dao.py)）：insert→`pending`（L22-48）→ `mark_parsing`→`parsing`（L50-59）→ `update_parse_result`→`completed`/`failed`（L61-77）。`none` 类型也会写成 `completed`（parsed_content=NULL，[file_parse_service.py:207-209](file:///workspace/app/services/file_parse_service.py#L207-L209)），不会停留 pending；`failed` 时 parsed_content 为失败文案（也会被注入，诚实告知）。
2. **注入点**：`_load_upload_context`（[orchestrator_service.py:917-941](file:///workspace/app/services/orchestrator_service.py#L917-L941)）仅查 `message_id IS NULL AND parsed_content 非空` 的记录，**解析中（pending/parsing）记录被直接过滤，无任何等待**。
3. **仅两个调用点**（grep 确认）：
   - L992：单 agent 短路路径（`agent_id` 直答）
   - L1099：编排路径（意图识别后、编排前汇合点）
4. **SSE 事件模式**：agent 真实事件以 `f"data: {event.model_dump_json()}\n\n"` 透传（base.py L133、orchestrator_service.py L747）；自定义事件用 `json.dumps` 构造（如 `bocha_sum`，base.py L177-187）。
5. **事件类实测**（已验证可实例化）：
   - `ToolCallStartEvent(reply_id, tool_call_id, tool_call_name)` → `{"type":"TOOL_CALL_START","reply_id":...,"tool_call_id":...,"tool_call_name":...,"metadata":{},"id":自动,"created_at":自动}`
   - `ToolCallEndEvent(reply_id, tool_call_id)` → `{"type":"TOOL_CALL_END",...}`
   - 导入源 `agentscope.event`（orchestrator_service.py L26 已从该模块导入，扩展即可）
   - 用真实事件类而非手工 dict，保证与真实工具调用线格式完全一致，前端零适配
6. **chat_service 事件循环**：只对 `error` 等类型特判，`TOOL_CALL_*` 原样透传前端，无需改动
7. **模块常量**：`_UPLOAD_CTX_HEADER` / `_UPLOAD_CTX_MAX_CHARS` 在 orchestrator_service.py L70-71；文件头部已 import asyncio/json，**缺 `import time` / `import uuid`**（需补）
8. **测试基建**：[tests/test_upload_inject_and_bind.py](file:///workspace/tests/test_upload_inject_and_bind.py) 有 `_FakeUploadDao`（async 方法假 DAO）与假 pool/cursor 两种模式

## Proposed Changes

### 1. DAO 层：[app/dao/upload_file_dao.py](file:///workspace/app/dao/upload_file_dao.py) 新增 `load_unbound_parsing`

放在 `load_unbound_parsed`（L79-96）之后：

```python
async def load_unbound_parsing(self, session_id: str) -> List[dict]:
    """查询该会话下未绑定消息且仍在解析中（pending/parsing）的上传文件。

    用于问答前轮询等待解析完成；none 类型会很快写成 completed，
    长期停留 pending/parsing 的一般是 mineru/asr 在途任务。
    """
    async with self.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT filename, parse_type FROM upload_files "
                "WHERE session_id = %s AND message_id IS NULL "
                "AND status IN ('pending', 'parsing') "
                "ORDER BY id",
                (session_id,),
            )
            rows = await cur.fetchall()
            await conn.commit()
            return [dict(r) for r in rows]
```

### 2. 服务层：[app/services/orchestrator_service.py](file:///workspace/app/services/orchestrator_service.py)

**2a. 模块头部**：补 `import time`、`import uuid`；L26 的 agentscope.event 导入扩展为：

```python
from agentscope.event import AgentEvent, ReplyStartEvent, ToolCallStartEvent, ToolCallEndEvent
```

**2b. 模块常量**（L70-71 旁追加）：

```python
# 提问时若上传文件仍在解析：轮询等待的总超时与间隔（秒）。
# 超时后不再等待，改为在提示词中注入解析失败提示。
_UPLOAD_WAIT_TIMEOUT = 15.0
_UPLOAD_WAIT_POLL_INTERVAL = 1.0

# 等待超时后仍在解析中的文件，按 parse_type 注入的失败提示文案
_UPLOAD_PARSE_TIMEOUT_HINTS = {
    "mineru": "解析超时，MinerU服务暂时无法解析该文件",
    "asr": "解析超时，音频解析服务暂时无法解析该文件",
}
_UPLOAD_PARSE_TIMEOUT_HINT_DEFAULT = "解析超时，暂时无法解析该文件"
```

**2c. 新增等待生成器**（放 `_load_upload_context` 前）：

```python
async def _wait_for_upload_parsing(
    self, request: Any, session_id: Optional[str]
) -> AsyncGenerator[str, None]:
    """存在解析中的未绑定上传文件时，轮询等待其完成（最多 _UPLOAD_WAIT_TIMEOUT 秒）。

    等待期间发一对 TOOL_CALL_START/TOOL_CALL_END 事件（tool_call_name
    "等待mineru文件解析完成"，id 随机造，仅用于前端展示）；
    DAO 异常静默结束（不等待、不发事件），不影响问答主流程。
    """
    if request is None or not session_id:
        return
    dao = getattr(request.app.state, "upload_file_dao", None)
    if dao is None:
        return
    try:
        parsing = await dao.load_unbound_parsing(session_id)
    except Exception:
        logger.warning("[OrchestratorService] 查询解析中上传文件失败", exc_info=True)
        return
    if not parsing:
        return

    reply_id = f"upload-wait-{uuid.uuid4().hex[:12]}"
    tool_call_id = f"upload-wait-{uuid.uuid4().hex[:12]}"
    yield (
        "data: "
        + ToolCallStartEvent(
            reply_id=reply_id,
            tool_call_id=tool_call_id,
            tool_call_name="等待mineru文件解析完成",
            metadata={"files": [r.get("filename", "") for r in parsing]},
        ).model_dump_json()
        + "\n\n"
    )
    deadline = time.monotonic() + _UPLOAD_WAIT_TIMEOUT
    while True:
        await asyncio.sleep(_UPLOAD_WAIT_POLL_INTERVAL)
        try:
            parsing = await dao.load_unbound_parsing(session_id)
        except Exception:
            logger.warning("[OrchestratorService] 轮询解析状态失败，停止等待", exc_info=True)
            break
        if not parsing:
            break
        if time.monotonic() >= deadline:
            break
    yield (
        "data: "
        + ToolCallEndEvent(
            reply_id=reply_id, tool_call_id=tool_call_id,
        ).model_dump_json()
        + "\n\n"
    )
```

要点：无解析中文件时**一个事件都不发**（普通路径零扰动）；START 与 END 复用同一对造出来的 id。

**2d. `_load_upload_context` 扩展**（L917-941）：在 `load_unbound_parsed` 之后追加查询 `load_unbound_parsing`，仍在解析中的（此时即等待超时的）文件按 parse_type 注入失败提示：

```python
        try:
            rows = await dao.load_unbound_parsed(session_id)
        except Exception:
            logger.warning("[OrchestratorService] 检索上传文件解析内容失败", exc_info=True)
            return ""
        # 等待超时后仍在解析中的文件：注入解析失败提示（agent 诚实告知用户）
        try:
            parsing_rows = await dao.load_unbound_parsing(session_id)
        except Exception:
            logger.warning("[OrchestratorService] 检索解析中上传文件失败", exc_info=True)
            parsing_rows = []
        if not rows and not parsing_rows:
            return ""
        parts = [_UPLOAD_CTX_HEADER]
        for row in rows:
            content = (row.get("parsed_content") or "")[:_UPLOAD_CTX_MAX_CHARS]
            parts.append(f"=== 文件名: {row.get('filename', '')} ===")
            parts.append(content)
        for row in parsing_rows:
            hint = _UPLOAD_PARSE_TIMEOUT_HINTS.get(
                row.get("parse_type"), _UPLOAD_PARSE_TIMEOUT_HINT_DEFAULT
            )
            parts.append(f"=== 文件名: {row.get('filename', '')} ===")
            parts.append(hint)
        return "\n".join(parts)
```

（空判断从 `if not rows` 改为 `if not rows and not parsing_rows`，否则仅剩超时文件时头部会丢。）

**2e. 两个调用点前插入等待**（不变更现有注入逻辑）：

- 单 agent 路径 L992 前：
```python
            async for ev in self._wait_for_upload_parsing(request, session_id):
                yield ev
            upload_ctx = await self._load_upload_context(request, session_id)
```
- 编排路径 L1099 前同理（`_load_upload_context` 调用之前）。

不变式：`_load_upload_context` 的两个调用点都先经过 `_wait_for_upload_parsing`，因此其中查到的 pending/parsing 记录必然是"等待超时"的。

### 3. 明确不改的部分

- `bind_message_id`（chat_service L284）：照旧绑定全部未绑定文件——超时文件本轮已注入失败提示，语义自洽
- `_has_unbound_uploads`（跳过问题改写判定）：不动
- file_parse_service：不动（其 60s 解析超时是解析侧，与本次 15s 等待超时是两层）
- chat_service 事件循环：`TOOL_CALL_*` 原样透传，无需特判
- .env / config.py：不动（用户要求常量写死在代码）

## Assumptions & Decisions

1. **等待对象为全部 pending/parsing 未绑定文件**（不只 mineru）：none 类型秒级 completed、plain_text 即时，实际在途的只有 mineru/asr；事件名按用户要求用 mineru 字样。
2. **超时提示按 parse_type 区分文案**（mineru/asr/默认），与 file_parse_service 现有超时文案风格一致；不复用其私有常量，本地定义避免耦合。
3. **事件用 agentscope 真实事件类构造**，与真实工具调用线格式（`data: {model_dump_json}\n\n`）完全一致，前端无需任何适配。
4. **造的 id 格式**：`upload-wait-{12位hex}`，reply_id 与 tool_call_id 各造一个，START/END 复用。
5. **轮询节奏**：先 sleep 再查（1s 间隔，15s 上限约 15 次查询）；DAO 异常立即停止等待、正常发 END 收尾。
6. **超时后提示词注入失败提示，但后台解析任务继续跑**（其结果照常入库，只是该文件已绑定不会再注入——与现状 bind 语义一致）。

## Verification

1. `python -m py_compile app/dao/upload_file_dao.py app/services/orchestrator_service.py`
2. `python -m pytest tests/test_upload_inject_and_bind.py -v`：新增用例 + 存量用例全绿
3. `python -m pytest tests/ -q`：全量回归无失败

### 新增测试（扩展 tests/test_upload_inject_and_bind.py）

- **DAO**：`load_unbound_parsing` SQL 含 `status IN ('pending', 'parsing')` + `message_id IS NULL`，参数正确，返回行
- **等待生成器**（monkeypatch `_UPLOAD_WAIT_TIMEOUT=0.2` / `_UPLOAD_WAIT_POLL_INTERVAL=0.05` 加速）：
  - 无解析中文件 / 无 dao / 无 session / DAO 抛异常 → 不 yield 任何事件
  - 有解析中文件→完成：恰好 2 个事件；第 1 个解析为 JSON 后 `type=="TOOL_CALL_START"`、`tool_call_name=="等待mineru文件解析完成"`、id 非空；第 2 个 `type=="TOOL_CALL_END"` 且 reply_id/tool_call_id 与 START 相同
  - 有解析中文件→始终未完成：超时后同样恰好 START+END 两个事件
  - 事件字符串格式：`data: ` 前缀 + `\n\n` 结尾
- **注入扩展**（`_FakeUploadDao` 补 `load_unbound_parsing` 方法，默认返回 `[]`）：
  - parsed + parsing 并存：ctx 同时含解析内容与超时提示，顺序 parsed 在前
  - 仅 parsing：ctx 含头部 + mineru/asr 对应提示文案
  - `load_unbound_parsing` 抛异常：仅注入 parsed，不报错
