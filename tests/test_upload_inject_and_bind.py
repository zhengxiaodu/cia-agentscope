"""上传文件解析内容注入提示词 + 问答结束回填 message_id 的单元测试。"""
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
    def __init__(self, rows=None, exc=None):
        self.rows = rows or []
        self.exc = exc
        self.bind_calls = []

    async def load_unbound_parsed(self, session_id):
        if self.exc:
            raise self.exc
        return self.rows

    async def bind_message_id(self, session_id, message_id):
        self.bind_calls.append((session_id, message_id))
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
# _persist_conversation_history：问答结束回填 message_id
# ---------------------------------------------------------------------------

def _make_session_service(user_message_id=101):
    svc = MagicMock()
    svc.append_messages = AsyncMock(return_value=user_message_id)
    return svc


_MESSAGES = [{"role": "user", "content": "总结这份文件"}]


@pytest.mark.asyncio
async def test_persist_binds_user_message_id():
    dao = _FakeUploadDao()
    session_service = _make_session_service(user_message_id=101)
    await _persist_conversation_history(
        None, session_service, "s1", "u1", _MESSAGES, "回答内容",
        upload_file_dao=dao,
    )
    assert dao.bind_calls == [("s1", 101)]


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
    dao.bind_message_id.assert_awaited_once_with("s1", 101)


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
