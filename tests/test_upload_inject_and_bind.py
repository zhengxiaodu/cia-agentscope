"""上传文件解析内容注入提示词 + 问答结束回填 message_id 的单元测试。"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import orchestrator_service as orch_mod
from app.services.orchestrator_service import OrchestratorService
from app.services.chat_service import _persist_conversation_history
from app.dao.upload_file_dao import UploadFileDAO


def _make_service() -> OrchestratorService:
    """绕过 __init__ 构造服务实例（被测方法不依赖实例状态）。"""
    return object.__new__(OrchestratorService)


def _make_request(dao=None):
    request = MagicMock()
    request.app.state.upload_file_dao = dao
    return request


class _FakeUploadDao:
    def __init__(self, rows=None, exc=None, parsing_rows=None):
        self.rows = rows or []
        self.exc = exc
        self.parsing_rows = parsing_rows or []
        self.bind_calls = []

    async def load_unbound_parsed(self, session_id):
        if self.exc:
            raise self.exc
        return self.rows

    async def load_unbound_parsing(self, session_id):
        return self.parsing_rows

    async def bind_message_id(self, session_id, message_id, message_pair_id=None):
        self.bind_calls.append((session_id, message_id, message_pair_id))
        return len(self.bind_calls)


# ---------------------------------------------------------------------------
# _load_upload_context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_upload_context_joins_files():
    dao = _FakeUploadDao(rows=[
        {"filename": "报告.pdf", "parsed_content": "# 报告内容"},
        {"filename": "语音.m4a", "parsed_content": "测试语音"},
    ])
    ctx = await _make_service()._load_upload_context(_make_request(dao), "s1")
    assert ctx.startswith(orch_mod._UPLOAD_CTX_HEADER)
    assert "=== 文件名: 报告.pdf ===" in ctx
    assert "# 报告内容" in ctx
    assert "=== 文件名: 语音.m4a ===" in ctx
    assert "测试语音" in ctx
    # 文件顺序与拼接顺序一致
    assert ctx.index("报告.pdf") < ctx.index("语音.m4a")


@pytest.mark.asyncio
async def test_load_upload_context_truncates_single_file():
    dao = _FakeUploadDao(rows=[
        {"filename": "big.pdf", "parsed_content": "x" * (orch_mod._UPLOAD_CTX_MAX_CHARS + 100)},
    ])
    ctx = await _make_service()._load_upload_context(_make_request(dao), "s1")
    body = ctx.split("=== 文件名: big.pdf ===\n", 1)[1]
    assert len(body) == orch_mod._UPLOAD_CTX_MAX_CHARS


@pytest.mark.asyncio
async def test_load_upload_context_empty_when_no_files():
    dao = _FakeUploadDao(rows=[])
    assert await _make_service()._load_upload_context(_make_request(dao), "s1") == ""


@pytest.mark.asyncio
async def test_load_upload_context_empty_when_no_session():
    dao = _FakeUploadDao(rows=[{"filename": "a.pdf", "parsed_content": "c"}])
    assert await _make_service()._load_upload_context(_make_request(dao), None) == ""
    assert await _make_service()._load_upload_context(_make_request(dao), "") == ""


@pytest.mark.asyncio
async def test_load_upload_context_empty_when_no_request():
    assert await _make_service()._load_upload_context(None, "s1") == ""


@pytest.mark.asyncio
async def test_load_upload_context_empty_when_dao_missing():
    assert await _make_service()._load_upload_context(_make_request(None), "s1") == ""


@pytest.mark.asyncio
async def test_load_upload_context_swallows_dao_exception():
    dao = _FakeUploadDao(exc=RuntimeError("db down"))
    assert await _make_service()._load_upload_context(_make_request(dao), "s1") == ""


# ---------------------------------------------------------------------------
# _append_upload_context
# ---------------------------------------------------------------------------

def test_append_upload_context_appends():
    result = OrchestratorService._append_upload_context("用户问题", "【文件】内容")
    assert result == "用户问题\n\n【文件】内容"


def test_append_upload_context_empty_ctx_untouched():
    assert OrchestratorService._append_upload_context("用户问题", "") == "用户问题"


# ---------------------------------------------------------------------------
# _load_upload_context：等待超时文件的失败提示注入
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_upload_context_includes_timeout_hints():
    """parsed 与 parsing 并存：解析内容在前，超时提示在后（按 parse_type 选文案）。"""
    dao = _FakeUploadDao(
        rows=[{"filename": "报告.pdf", "parsed_content": "# 报告内容"}],
        parsing_rows=[
            {"filename": "大文件.pdf", "parse_type": "mineru"},
            {"filename": "语音.m4a", "parse_type": "asr"},
        ],
    )
    ctx = await _make_service()._load_upload_context(_make_request(dao), "s1")
    assert ctx.startswith(orch_mod._UPLOAD_CTX_HEADER)
    # parsed 在前
    assert "=== 文件名: 报告.pdf ===" in ctx
    assert "# 报告内容" in ctx
    # 超时提示按 parse_type 区分文案
    assert "=== 文件名: 大文件.pdf ===\n解析超时，MinerU服务暂时无法解析该文件" in ctx
    assert "=== 文件名: 语音.m4a ===\n解析超时，音频解析服务暂时无法解析该文件" in ctx
    # 顺序：parsed 条目在 parsing 条目之前
    assert ctx.index("报告.pdf") < ctx.index("大文件.pdf")


@pytest.mark.asyncio
async def test_load_upload_context_only_parsing_files():
    """仅剩解析中超时文件：也生成头部与失败提示。"""
    dao = _FakeUploadDao(
        parsing_rows=[{"filename": "大文件.pdf", "parse_type": "mineru"}],
    )
    ctx = await _make_service()._load_upload_context(_make_request(dao), "s1")
    assert ctx.startswith(orch_mod._UPLOAD_CTX_HEADER)
    assert "=== 文件名: 大文件.pdf ===" in ctx
    assert "解析超时，MinerU服务暂时无法解析该文件" in ctx


@pytest.mark.asyncio
async def test_load_upload_context_unknown_parse_type_default_hint():
    """未知 parse_type：使用默认提示文案。"""
    dao = _FakeUploadDao(
        parsing_rows=[{"filename": "a.bin", "parse_type": "whatever"}],
    )
    ctx = await _make_service()._load_upload_context(_make_request(dao), "s1")
    assert orch_mod._UPLOAD_PARSE_TIMEOUT_HINT_DEFAULT in ctx


@pytest.mark.asyncio
async def test_load_upload_context_swallows_parsing_dao_exception():
    """load_unbound_parsing 抛异常：只注入 parsed 内容，不报错。"""
    dao = _FakeUploadDao(rows=[{"filename": "a.pdf", "parsed_content": "内容"}])

    async def _raise(session_id):
        raise RuntimeError("db down")

    dao.load_unbound_parsing = _raise
    ctx = await _make_service()._load_upload_context(_make_request(dao), "s1")
    assert "=== 文件名: a.pdf ===" in ctx
    assert "内容" in ctx


# ---------------------------------------------------------------------------
# _wait_for_upload_parsing：轮询等待 + TOOL_CALL 事件对
# ---------------------------------------------------------------------------

class _ScriptedParsingDao:
    """load_unbound_parsing 按脚本依次返回结果（耗尽后重复最后一项）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def load_unbound_parsing(self, session_id):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        result = self.script[idx]
        if isinstance(result, Exception):
            raise result
        return result


def _parse_sse(raw: str) -> dict:
    """去掉 data: 前缀与空行，解析 JSON。"""
    assert raw.startswith("data: ") and raw.endswith("\n\n"), raw
    return json.loads(raw[len("data: "):-2])


@pytest.mark.parametrize("dao,session_id", [
    (_FakeUploadDao(parsing_rows=[]), "s1"),          # 无解析中文件
    (_FakeUploadDao(parsing_rows=[{"filename": "a.pdf", "parse_type": "mineru"}]), None),
    (_FakeUploadDao(parsing_rows=[{"filename": "a.pdf", "parse_type": "mineru"}]), ""),
])
@pytest.mark.asyncio
async def test_wait_no_events_without_parsing_files(dao, session_id):
    """无解析中文件/无会话：不 yield 任何事件（普通路径零扰动）。"""
    events = [
        ev async for ev in _make_service()._wait_for_upload_parsing(
            _make_request(dao), session_id,
        )
    ]
    assert events == []


@pytest.mark.asyncio
async def test_wait_no_events_when_dao_missing():
    assert [
        ev async for ev in _make_service()._wait_for_upload_parsing(_make_request(None), "s1")
    ] == []


@pytest.mark.asyncio
async def test_wait_no_events_when_dao_raises():
    dao = _ScriptedParsingDao([RuntimeError("db down")])
    assert [
        ev async for ev in _make_service()._wait_for_upload_parsing(_make_request(dao), "s1")
    ] == []


@pytest.mark.asyncio
async def test_wait_emits_start_end_pair_on_completion(monkeypatch):
    """解析中→完成：恰好 START+END 两个事件，id 复用、字段正确。"""
    monkeypatch.setattr(orch_mod, "_UPLOAD_WAIT_TIMEOUT", 5.0)
    monkeypatch.setattr(orch_mod, "_UPLOAD_WAIT_POLL_INTERVAL", 0.01)
    dao = _ScriptedParsingDao([
        [{"filename": "a.pdf", "parse_type": "mineru"}],  # 首查：解析中
        [],                                               # 第一次轮询：已完成
    ])

    events = [
        ev async for ev in _make_service()._wait_for_upload_parsing(_make_request(dao), "s1")
    ]

    assert len(events) == 2
    start, end = _parse_sse(events[0]), _parse_sse(events[1])
    assert start["type"] == "TOOL_CALL_START"
    assert start["tool_call_name"] == "等待mineru文件解析完成"
    assert start["reply_id"] and start["tool_call_id"]
    assert start["reply_id"].startswith("upload-wait-")
    assert start["tool_call_id"].startswith("upload-wait-")
    assert start["metadata"]["files"] == ["a.pdf"]
    assert end["type"] == "TOOL_CALL_END"
    # END 复用 START 的 reply_id / tool_call_id
    assert end["reply_id"] == start["reply_id"]
    assert end["tool_call_id"] == start["tool_call_id"]


@pytest.mark.asyncio
async def test_wait_times_out_and_emits_end(monkeypatch):
    """始终未完成：超时后停止轮询并发 END（同样恰好两个事件）。"""
    monkeypatch.setattr(orch_mod, "_UPLOAD_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr(orch_mod, "_UPLOAD_WAIT_POLL_INTERVAL", 0.01)
    dao = _ScriptedParsingDao([
        [{"filename": "a.pdf", "parse_type": "mineru"}],  # 永远解析中
    ])

    events = [
        ev async for ev in _make_service()._wait_for_upload_parsing(_make_request(dao), "s1")
    ]

    assert len(events) == 2
    start, end = _parse_sse(events[0]), _parse_sse(events[1])
    assert start["type"] == "TOOL_CALL_START"
    assert end["type"] == "TOOL_CALL_END"
    assert end["tool_call_id"] == start["tool_call_id"]
    # 超时前轮询了多次（1 次首查 + 若干次轮询）
    assert dao.calls >= 2


@pytest.mark.asyncio
async def test_wait_stops_polling_on_dao_error(monkeypatch):
    """轮询中 DAO 异常：立即停止等待，END 事件仍发出收尾。"""
    monkeypatch.setattr(orch_mod, "_UPLOAD_WAIT_TIMEOUT", 5.0)
    monkeypatch.setattr(orch_mod, "_UPLOAD_WAIT_POLL_INTERVAL", 0.01)
    dao = _ScriptedParsingDao([
        [{"filename": "a.pdf", "parse_type": "mineru"}],  # 首查：解析中
        RuntimeError("db down"),                          # 第一次轮询：异常
    ])

    events = [
        ev async for ev in _make_service()._wait_for_upload_parsing(_make_request(dao), "s1")
    ]

    assert len(events) == 2
    assert _parse_sse(events[0])["type"] == "TOOL_CALL_START"
    assert _parse_sse(events[1])["type"] == "TOOL_CALL_END"
    assert dao.calls == 2  # 异常后不再轮询


# ---------------------------------------------------------------------------
# UploadFileDAO.load_unbound_parsing（mock aiomysql 连接池）
# ---------------------------------------------------------------------------

class _RowsCursor:
    def __init__(self, rows, log):
        self._rows = rows
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, args=None):
        self._log.append((sql, args))

    async def fetchall(self):
        return self._rows


class _RowsConn:
    def __init__(self, rows, log):
        self._rows = rows
        self._log = log

    def cursor(self, cursor_cls=None):
        return _RowsCursor(self._rows, self._log)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def commit(self):
        pass


class _RowsPool:
    def __init__(self, rows):
        self._rows = rows
        self.log = []

    def acquire(self):
        return _RowsConn(self._rows, self.log)


@pytest.mark.asyncio
async def test_load_unbound_parsing_sql_and_rows():
    rows = [
        {"filename": "a.pdf", "parse_type": "mineru"},
        {"filename": "b.m4a", "parse_type": "asr"},
    ]
    pool = _RowsPool(rows)
    dao = UploadFileDAO(pool)

    result = await dao.load_unbound_parsing("s1")

    sql, args = pool.log[0]
    assert "message_id IS NULL" in sql
    assert "status IN ('pending', 'parsing')" in sql
    assert "ORDER BY id" in sql
    assert args == ("s1",)
    assert result == rows


@pytest.mark.asyncio
async def test_load_unbound_parsing_empty():
    pool = _RowsPool([])
    dao = UploadFileDAO(pool)
    assert await dao.load_unbound_parsing("s1") == []


# ---------------------------------------------------------------------------
# _persist_conversation_history：问答结束回填 message_id
# ---------------------------------------------------------------------------

def _make_session_service(user_message_id=101, assistant_message_id=102):
    """append_messages 现返回 {"user_message_id", "assistant_message_id"} dict。"""
    svc = MagicMock()
    svc.append_messages = AsyncMock(return_value={
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
    })
    return svc


_MESSAGES = [{"role": "user", "content": "总结这份文件"}]


@pytest.mark.asyncio
async def test_persist_binds_user_message_id():
    dao = _FakeUploadDao()
    session_service = _make_session_service(user_message_id=101)
    await _persist_conversation_history(
        None, session_service, "s1", "u1", _MESSAGES, "回答内容",
        upload_file_dao=dao,
        message_pair_id="pair-1",
    )
    # bind_message_id 三参调用：session_id + user_message_id + message_pair_id
    assert dao.bind_calls == [("s1", 101, "pair-1")]


@pytest.mark.asyncio
async def test_persist_skips_bind_when_no_user_message_id():
    dao = _FakeUploadDao()
    session_service = _make_session_service(user_message_id=None)
    await _persist_conversation_history(
        None, session_service, "s1", "u1", _MESSAGES, "回答内容",
        upload_file_dao=dao,
    )
    assert dao.bind_calls == []


@pytest.mark.asyncio
async def test_persist_skips_bind_when_dao_missing():
    session_service = _make_session_service(user_message_id=101)
    # upload_file_dao 为 None：不回填、不报错
    await _persist_conversation_history(
        None, session_service, "s1", "u1", _MESSAGES, "回答内容",
        upload_file_dao=None,
    )


@pytest.mark.asyncio
async def test_persist_swallows_bind_exception():
    dao = MagicMock()
    dao.bind_message_id = AsyncMock(side_effect=RuntimeError("db down"))
    session_service = _make_session_service(user_message_id=101)
    # bind 抛异常不影响主流程（不向外抛）
    await _persist_conversation_history(
        None, session_service, "s1", "u1", _MESSAGES, "回答内容",
        upload_file_dao=dao,
    )
    # 未传 message_pair_id 时第三参为 None
    dao.bind_message_id.assert_awaited_once_with("s1", 101, None)


# ---------------------------------------------------------------------------
# UploadFileDAO.has_unbound_files（mock aiomysql 连接池）
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, row, log):
        self._row = row
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, args=None):
        self._log.append((sql, args))

    async def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row, log):
        self._row = row
        self._log = log

    def cursor(self, cursor_cls=None):
        return _FakeCursor(self._row, self._log)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def commit(self):
        pass


class _FakePool:
    def __init__(self, row):
        self._row = row
        self.log = []

    def acquire(self):
        return _FakeConn(self._row, self.log)


@pytest.mark.asyncio
async def test_has_unbound_files_true():
    pool = _FakePool(row={"1": 1})
    dao = UploadFileDAO(pool)
    assert await dao.has_unbound_files("s1") is True
    sql, args = pool.log[0]
    assert "message_id IS NULL" in sql
    assert args == ("s1",)


@pytest.mark.asyncio
async def test_has_unbound_files_false():
    pool = _FakePool(row=None)
    dao = UploadFileDAO(pool)
    assert await dao.has_unbound_files("s1") is False


# ---------------------------------------------------------------------------
# _has_unbound_uploads（跳过问题改写的判定）
# ---------------------------------------------------------------------------

class _HasFilesDao:
    def __init__(self, result, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    async def has_unbound_files(self, session_id):
        self.calls.append(session_id)
        if self.exc:
            raise self.exc
        return self.result


@pytest.mark.asyncio
async def test_has_unbound_uploads_true():
    dao = _HasFilesDao(result=True)
    assert await _make_service()._has_unbound_uploads(_make_request(dao), "s1") is True
    assert dao.calls == ["s1"]


@pytest.mark.asyncio
async def test_has_unbound_uploads_false():
    dao = _HasFilesDao(result=False)
    assert await _make_service()._has_unbound_uploads(_make_request(dao), "s1") is False


@pytest.mark.asyncio
async def test_has_unbound_uploads_no_session_or_request():
    dao = _HasFilesDao(result=True)
    assert await _make_service()._has_unbound_uploads(_make_request(dao), None) is False
    assert await _make_service()._has_unbound_uploads(_make_request(dao), "") is False
    assert await _make_service()._has_unbound_uploads(None, "s1") is False
    assert dao.calls == []


@pytest.mark.asyncio
async def test_has_unbound_uploads_no_dao():
    assert await _make_service()._has_unbound_uploads(_make_request(None), "s1") is False


@pytest.mark.asyncio
async def test_has_unbound_uploads_swallows_exception():
    dao = _HasFilesDao(result=True, exc=RuntimeError("db down"))
    assert await _make_service()._has_unbound_uploads(_make_request(dao), "s1") is False
