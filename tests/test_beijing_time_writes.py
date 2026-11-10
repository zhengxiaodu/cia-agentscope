"""时间字段统一北京时间测试：DAO 写入时间口径 + 消息时间戳 + 连接池会话时区。

Python 侧统一按东八区生成（_BEIJING_TZ），MySQL 连接池固定会话时区 +08:00
（NOW()/CURRENT_TIMESTAMP 与 Python 侧对齐）。
"""
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.dao.mysql_session_dao import _BEIJING_TZ as DAO_TZ, SessionDAO
from app.services.chat_service import (
    _BEIJING_TZ as CHAT_TZ,
    _persist_conversation_history,
)
from unittest.mock import AsyncMock, MagicMock

_EXPECTED_OFFSET = timedelta(hours=8)


def _assert_beijing_now(dt):
    """断言 dt 为东八区当前时间（允许 2 秒执行误差）。"""
    assert dt is not None
    assert dt.utcoffset() == _EXPECTED_OFFSET
    delta = abs((dt - datetime.now(DAO_TZ)).total_seconds())
    assert delta < 2, f"时间偏差 {delta}s"


# ---------------------------------------------------------------------------
# 假 pool / conn / cursor（fetchone 按队列返回，lastrowid 可控）
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, fetchone_results=None, lastrowid=101):
        self._fetchone_results = list(fetchone_results or [])
        self.executed = []
        self.lastrowid = lastrowid
        self.rowcount = 0

    async def execute(self, sql, args=None):
        self.executed.append((sql, args))

    async def fetchone(self):
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return None

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor_obj):
        self.cursor_obj = cursor_obj

    def cursor(self, cursor_class=None):
        return self.cursor_obj

    async def begin(self):
        pass

    async def commit(self):
        pass

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
# 常量口径
# ---------------------------------------------------------------------------


def test_beijing_tz_constants():
    """DAO 与 chat_service 的时区常量均为 UTC+8。"""
    assert DAO_TZ.utcoffset(None) == _EXPECTED_OFFSET
    assert CHAT_TZ.utcoffset(None) == _EXPECTED_OFFSET


# ---------------------------------------------------------------------------
# DAO 写入时间为北京时间
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_session_writes_beijing_time():
    """pin_session 的 pinned_at/updated_at 参数为东八区当前时间。"""
    cur = _FakeCursor()
    dao = SessionDAO(_FakePool(cur))

    await dao.pin_session("u1", "s1")

    sql, args = cur.executed[0]
    assert "is_pinned = 1" in sql
    _assert_beijing_now(args[0])
    _assert_beijing_now(args[1])


@pytest.mark.asyncio
async def test_save_agent_state_writes_beijing_time():
    """sessions INSERT 的 created_at/updated_at 与 agent_states 的 updated_at 均为北京时间。"""
    # fetchone 队列：sessions 不存在 → INSERT 路径
    cur = _FakeCursor(fetchone_results=[None])
    dao = SessionDAO(_FakePool(cur))

    await dao.save_agent_state("s1", "u1", "general_agent", {"k": "v"})

    # executed: [SELECT sessions, INSERT sessions, INSERT agent_states]
    insert_sessions = cur.executed[1]
    assert "INSERT INTO sessions" in insert_sessions[0]
    _assert_beijing_now(insert_sessions[1][3])  # created_at
    _assert_beijing_now(insert_sessions[1][4])  # updated_at

    insert_states = cur.executed[2]
    assert "INSERT INTO agent_states" in insert_states[0]
    _assert_beijing_now(insert_states[1][3])  # updated_at


@pytest.mark.asyncio
async def test_append_messages_default_timestamp_beijing():
    """消息不带 timestamp 时落库时间默认为东八区当前时间。"""
    # fetchone 队列：sessions 不存在 → INSERT 路径；随后 COUNT 返回 2
    cur = _FakeCursor(fetchone_results=[None, {"cnt": 2}])
    dao = SessionDAO(_FakePool(cur))

    result = await dao.append_messages("s1", "u1", [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "回复"},
    ])

    assert result == {"user_message_id": 101, "assistant_message_id": 101}
    # 找到两条 INSERT INTO messages，timestamp 参数（args[3]）均为北京时间
    msg_inserts = [e for e in cur.executed if "INSERT INTO messages" in e[0]]
    assert len(msg_inserts) == 2
    for _, args in msg_inserts:
        _assert_beijing_now(args[3])


@pytest.mark.asyncio
async def test_append_messages_parsed_timestamp_beijing_tz():
    """消息自带 timestamp 字符串时按东八区语义解析（不再按 UTC）。"""
    cur = _FakeCursor(fetchone_results=[None, {"cnt": 1}])
    dao = SessionDAO(_FakePool(cur))

    await dao.append_messages("s1", "u1", [
        {"role": "user", "content": "你好", "timestamp": "2026-09-15 10:00:00.000"},
    ])

    msg_inserts = [e for e in cur.executed if "INSERT INTO messages" in e[0]]
    ts = msg_inserts[0][1][3]
    assert ts.utcoffset() == _EXPECTED_OFFSET
    # 字面值不变（仍是 10:00），仅时区语义变为 +08:00
    assert ts.replace(tzinfo=None) == datetime(2026, 9, 15, 10, 0, 0)


# ---------------------------------------------------------------------------
# chat_service 消息时间戳字符串
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_history_timestamp_string_beijing():
    """_persist_conversation_history 生成的 timestamp 字符串为北京时间字面值。"""
    service = MagicMock()
    service.append_messages = AsyncMock(
        return_value={"user_message_id": 1, "assistant_message_id": 2}
    )

    result = await _persist_conversation_history(
        orchestrator_service=None,
        session_service=service,
        session_id="s1",
        user_id="u1",
        messages=[{"role": "user", "content": "你好"}],
        final_output="回复内容",
    )

    assert result == {"user_message_id": 1, "assistant_message_id": 2}
    args = service.append_messages.await_args[0]
    new_messages = args[2]
    assert len(new_messages) == 2
    for msg in new_messages:
        ts = datetime.strptime(msg["timestamp"], "%Y-%m-%d %H:%M:%S.%f")
        delta = abs((ts - datetime.now(CHAT_TZ).replace(tzinfo=None)).total_seconds())
        assert delta < 2, f"时间偏差 {delta}s"


# ---------------------------------------------------------------------------
# main.py 连接池会话时区（静态断言，不真实连库）
# ---------------------------------------------------------------------------


def test_mysql_pool_session_timezone_fixed():
    """连接池 init_command 固定会话时区为 +08:00（NOW()/CURRENT_TIMESTAMP 对齐）。"""
    import app.main as main_mod

    src = open(main_mod.__file__, encoding="utf-8").read()
    m = re.search(r"aiomysql\.create_pool\((.*?)\)", src, re.DOTALL)
    assert m, "未找到 aiomysql.create_pool 调用"
    assert 'init_command="SET time_zone = \'+08:00\'"' in m.group(1)
