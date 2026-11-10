"""生成文件持久化测试：session_files 新列写库/读取 + 字节流搬运落盘。

覆盖：
1. SessionDAO.append_session_files：files dict 带 message_id/message_pair_id
   时 INSERT（UPSERT）SQL 与参数正确；load_session_files 返回带新列的行映射
2. chat_service._detect_and_emit_files：持久化成功（tmp 目录真实落盘、保留
   子目录、url=/persist-files/...、事件 payload 与 session_files 记录带新字段）、
   读取失败回退沙箱 url、未配置持久化目录回退且不读沙箱字节流
3. chat_service._persist_file_bytes：正常写入 / 异常（makedirs 失败）返回 False
"""
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_service as chat_service_module
from app.dao.mysql_session_dao import SessionDAO
from app.services.chat_service import _detect_and_emit_files, _persist_file_bytes


# ---------------------------------------------------------------------------
# 假 pool / conn / cursor（风格对齐 test_upload_query.py）
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.executed = []

    async def execute(self, sql, args=None):
        self.executed.append((sql, args))

    async def fetchall(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor_obj):
        self.cursor_obj = cursor_obj
        self.begun = False
        self.committed = False

    def cursor(self, cursor_class=None):
        return self.cursor_obj

    async def begin(self):
        self.begun = True

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, cursor_obj):
        self.conn = _FakeConn(cursor_obj)

    def acquire(self):
        return self.conn


# ---------------------------------------------------------------------------
# SessionDAO.append_session_files / load_session_files
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_session_files_writes_message_id_and_pair_id():
    """INSERT（UPSERT）携带 message_id / message_pair_id 两列并同步 UPDATE。"""
    cur = _FakeCursor()
    dao = SessionDAO(_FakePool(cur))
    files = [
        {"name": "a.md", "path": "a.md", "url": "/persist-files/s1/a.md",
         "size": 12, "media_type": "text/markdown",
         "message_id": 102, "message_pair_id": "pair-1"},
        {"name": "old.txt", "path": "old.txt", "url": "/files/s1/old.txt",
         "size": 3, "media_type": "text/plain",
         "message_id": None, "message_pair_id": None},
    ]

    await dao.append_session_files("s1", files)

    assert dao.pool.conn.begun
    assert dao.pool.conn.committed
    inserts = [
        (sql, args) for sql, args in cur.executed
        if sql.startswith("INSERT INTO session_files")
    ]
    assert len(inserts) == 2
    for sql, _ in inserts:
        assert "message_id, message_pair_id" in sql
        assert "message_id = VALUES(message_id)" in sql
        assert "message_pair_id = VALUES(message_pair_id)" in sql

    _, args1 = inserts[0]
    assert args1 == ("s1", "a.md", "a.md", "/persist-files/s1/a.md", 12,
                     "text/markdown", 102, "pair-1")
    _, args2 = inserts[1]
    assert args2 == ("s1", "old.txt", "old.txt", "/files/s1/old.txt", 3,
                     "text/plain", None, None)


@pytest.mark.asyncio
async def test_append_session_files_empty_list_noop():
    """空 files 列表直接返回，不执行任何 SQL。"""
    cur = _FakeCursor()
    dao = SessionDAO(_FakePool(cur))

    await dao.append_session_files("s1", [])

    assert cur.executed == []
    assert dao.pool.conn.begun is False


@pytest.mark.asyncio
async def test_load_session_files_maps_new_columns():
    """load_session_files 返回 message_id / message_pair_id（旧记录为 None）。"""
    rows = [
        {"name": "a.md", "path": "a.md", "url": "/persist-files/s1/a.md",
         "size": 12, "media_type": "text/markdown",
         "created_at": datetime(2026, 1, 1, 12, 0, 0),
         "message_id": 102, "message_pair_id": "pair-1"},
        {"name": "old.txt", "path": "old.txt", "url": "/files/s1/old.txt",
         "size": 3, "media_type": "text/plain",
         "created_at": datetime(2026, 1, 1, 11, 0, 0),
         "message_id": None, "message_pair_id": None},
    ]
    cur = _FakeCursor(rows=rows)
    dao = SessionDAO(_FakePool(cur))

    result = await dao.load_session_files("s1")

    sql, args = cur.executed[0]
    assert "message_id, message_pair_id" in sql
    assert args == ("s1",)

    assert result[0]["message_id"] == 102
    assert result[0]["message_pair_id"] == "pair-1"
    assert result[0]["created_at"] == "2026-01-01 12:00:00.000"
    assert result[1]["message_id"] is None
    assert result[1]["message_pair_id"] is None


# ---------------------------------------------------------------------------
# _persist_file_bytes
# ---------------------------------------------------------------------------

def test_persist_file_bytes_writes_and_keeps_subdirs(tmp_path):
    """正常写入：保留 rel_path 子目录结构，返回 True。"""
    ok = _persist_file_bytes(str(tmp_path), "s1", "sub/a.md", b"hello")

    assert ok is True
    assert (tmp_path / "s1" / "sub" / "a.md").read_bytes() == b"hello"
    assert (tmp_path / "s1" / "sub").is_dir()


def test_persist_file_bytes_returns_false_on_error(tmp_path):
    """异常路径：persist_dir 指向一个已存在文件 → makedirs 失败 → 返回 False。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是一个文件，不是目录")

    ok = _persist_file_bytes(str(blocker), "s1", "a.md", b"hello")

    assert ok is False


# ---------------------------------------------------------------------------
# _detect_and_emit_files
# ---------------------------------------------------------------------------

def _make_ws(list_files=None, stat_size=5, read_content=b"hello"):
    ws = MagicMock()
    ws.list_session_files = AsyncMock(return_value=set(list_files or []))
    ws.stat_session_file = AsyncMock(return_value=stat_size)
    ws.read_session_file = AsyncMock(return_value=read_content)
    return ws


async def _consume_detect(session_id, before, session_service, ws,
                           message_pair_id="pair-1", assistant_message_id=102):
    return [
        ev async for ev in _detect_and_emit_files(
            session_id, before, session_service,
            workspace_manager=ws, user_id="u1",
            message_pair_id=message_pair_id,
            assistant_message_id=assistant_message_id,
        )
    ]


def _parse_event(ev):
    assert ev.startswith("data: ") and ev.endswith("\n\n")
    return json.loads(ev[6:].strip())


@pytest.mark.asyncio
async def test_detect_and_emit_files_persist_success(monkeypatch, tmp_path):
    """持久化成功：字节流落盘 tmp 目录（保留子目录）、url 指向 /persist-files/、
    事件 payload 与 session_files 记录携带 message_id / message_pair_id。"""
    monkeypatch.setattr(chat_service_module, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    ws = _make_ws(list_files=["a.md", "sub/b.docx"], stat_size=5)
    session_service = MagicMock()
    session_service.append_session_files = AsyncMock()

    events = await _consume_detect("s1", set(), session_service, ws)

    assert len(events) == 1
    payload = _parse_event(events[0])
    assert payload["type"] == "files_generated"
    # diff 结果按路径升序
    files = payload["files"]
    assert [f["path"] for f in files] == ["a.md", "sub/b.docx"]
    by_path = {f["path"]: f for f in files}
    assert by_path["a.md"]["url"] == "/persist-files/s1/a.md"
    assert by_path["a.md"]["name"] == "a.md"
    assert by_path["a.md"]["size"] == 5
    assert by_path["sub/b.docx"]["url"] == "/persist-files/s1/sub/b.docx"
    assert by_path["sub/b.docx"]["name"] == "b.docx"
    # payload 每项附带本轮 assistant 消息 id 与配对 id
    assert all(f["message_id"] == 102 for f in files)
    assert all(f["message_pair_id"] == "pair-1" for f in files)

    # 字节流真实落盘，子目录结构保留
    assert (tmp_path / "s1" / "a.md").read_bytes() == b"hello"
    assert (tmp_path / "s1" / "sub" / "b.docx").read_bytes() == b"hello"
    assert (tmp_path / "s1" / "sub").is_dir()

    # session_files 记录带新字段
    session_service.append_session_files.assert_awaited_once()
    sid, files_arg = session_service.append_session_files.await_args[0]
    assert sid == "s1"
    by_path_arg = {f["path"]: f for f in files_arg}
    assert by_path_arg["a.md"]["url"] == "/persist-files/s1/a.md"
    assert by_path_arg["a.md"]["message_id"] == 102
    assert by_path_arg["a.md"]["message_pair_id"] == "pair-1"
    assert by_path_arg["sub/b.docx"]["message_id"] == 102
    assert by_path_arg["sub/b.docx"]["message_pair_id"] == "pair-1"


@pytest.mark.asyncio
async def test_detect_and_emit_files_fallback_when_read_fails(monkeypatch, tmp_path):
    """读取沙箱字节流失败（read 返回 None）：回退沙箱 url，不落盘、不中断。"""
    monkeypatch.setattr(chat_service_module, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    ws = _make_ws(list_files=["a.md"], read_content=None)
    session_service = MagicMock()
    session_service.append_session_files = AsyncMock()

    events = await _consume_detect("s1", set(), session_service, ws)

    payload = _parse_event(events[0])
    files = payload["files"]
    assert len(files) == 1
    assert files[0]["url"] == "/files/s1/a.md"
    # 回退时新字段照常记录
    assert files[0]["message_id"] == 102
    assert files[0]["message_pair_id"] == "pair-1"
    # 未写入持久化目录
    assert not (tmp_path / "s1").exists()
    # session_files 照常落库（沙箱 url）
    session_service.append_session_files.assert_awaited_once()
    files_arg = session_service.append_session_files.await_args[0][1]
    assert files_arg[0]["url"] == "/files/s1/a.md"


@pytest.mark.asyncio
async def test_detect_and_emit_files_fallback_when_not_configured(monkeypatch, tmp_path):
    """未配置持久化目录（空串）：不读沙箱字节流，url 回退 /files/...。"""
    monkeypatch.setattr(chat_service_module, "SESSION_FILES_PERSIST_DIR", "")
    ws = _make_ws(list_files=["a.md"])
    session_service = MagicMock()
    session_service.append_session_files = AsyncMock()

    events = await _consume_detect("s1", set(), session_service, ws)

    payload = _parse_event(events[0])
    files = payload["files"]
    assert len(files) == 1
    assert files[0]["url"] == "/files/s1/a.md"
    # 未配置目录时不发起字节流读取
    ws.read_session_file.assert_not_awaited()
    assert not (tmp_path / "s1").exists()
    session_service.append_session_files.assert_awaited_once()


@pytest.mark.asyncio
async def test_detect_and_emit_files_only_new_files_persisted(monkeypatch, tmp_path):
    """快照差集：before 中已存在的文件不重复持久化。"""
    monkeypatch.setattr(chat_service_module, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    ws = _make_ws(list_files=["a.md", "b.md"])
    session_service = MagicMock()
    session_service.append_session_files = AsyncMock()

    events = await _consume_detect("s1", {"a.md"}, session_service, ws)

    payload = _parse_event(events[0])
    assert [f["path"] for f in payload["files"]] == ["b.md"]
    # 只有新文件 b.md 落盘
    assert not (tmp_path / "s1" / "a.md").exists()
    assert (tmp_path / "s1" / "b.md").read_bytes() == b"hello"
