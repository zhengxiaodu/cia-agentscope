"""PUT /sessions/{session_id}/name 修改会话名称测试：DAO SQL + 路由参数校验。

风格对齐 test_sessions_pagination.py：DAO 用假 pool/cursor 验证 SQL 与参数；
路由自建最小 app（真 JWT 签发 + mock session_service）。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.mysql_session_dao import SessionDAO
from app.routes import sessions as sessions_route
from app.services.auth_service import create_access_token


# ---------------------------------------------------------------------------
# DAO：假 pool / conn / cursor（rowcount 可控）
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.executed = []

    async def execute(self, sql, args=None):
        self.executed.append((sql, args))

    async def fetchone(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor_obj):
        self.cursor_obj = cursor_obj
        self.committed = False

    def cursor(self, cursor_class=None):
        return self.cursor_obj

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, cursor_obj):
        self.conn = _FakeConn(cursor_obj)

    def acquire(self):
        return self.conn


def _make_dao(rowcount=1):
    cur = _FakeCursor(rowcount=rowcount)
    dao = SessionDAO(_FakePool(cur))
    return dao, cur


# ---------------------------------------------------------------------------
# DAO 层测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dao_rename_sql_and_args():
    """SQL 仅更新 name（不含 updated_at），参数顺序 (name, session_id, user_id)。"""
    dao, cur = _make_dao(rowcount=1)

    ok = await dao.rename_session("u1", "s1", "新名字")

    assert ok is True
    assert len(cur.executed) == 1
    sql, args = cur.executed[0]
    assert sql == (
        "UPDATE sessions SET name = %s "
        "WHERE session_id = %s AND user_id = %s"
    )
    assert "updated_at" not in sql
    assert args == ("新名字", "s1", "u1")


@pytest.mark.asyncio
async def test_dao_rename_not_found_returns_false():
    """rowcount=0（会话不存在或不属于该用户）→ False。"""
    dao, cur = _make_dao(rowcount=0)

    ok = await dao.rename_session("u1", "missing", "新名字")

    assert ok is False
    assert len(cur.executed) == 1


# ---------------------------------------------------------------------------
# 路由层：PUT /sessions/{session_id}/name
# ---------------------------------------------------------------------------


def _make_app(service=None) -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_route.router)
    if service is None:
        service = MagicMock()
        service.rename_session = AsyncMock(return_value=True)
    app.state.session_service = service
    return app


def _auth_headers() -> dict:
    token = create_access_token({"user_id": "u1", "department": "研发部"})
    return {"Authorization": f"Bearer {token}"}


def _make_service(ok=True):
    service = MagicMock()
    service.rename_session = AsyncMock(return_value=ok)
    return service


def test_rename_success():
    """正常改名 → 200，返回 {session_id, name}，service 收到 strip 后的名称。"""
    service = _make_service(ok=True)
    client = TestClient(_make_app(service))

    resp = client.put(
        "/sessions/s1/name",
        json={"name": "  新会话名  "},
        headers=_auth_headers(),
    )

    assert resp.status_code == 200
    service.rename_session.assert_awaited_once_with("u1", "s1", "新会话名")
    body = resp.json()
    assert body["code"] == 200
    assert body["data"] == {"session_id": "s1", "name": "新会话名"}


def test_rename_empty_name_rejected():
    """name 为空 / 全空格 / 缺失 → 400，不触达 service。"""
    service = _make_service()
    client = TestClient(_make_app(service))

    for payload in ({"name": ""}, {"name": "   "}, {}):
        resp = client.put(
            "/sessions/s1/name", json=payload, headers=_auth_headers(),
        )
        assert resp.status_code == 200, payload
        assert resp.json()["code"] == 400, payload

    service.rename_session.assert_not_awaited()


def test_rename_too_long_name_rejected():
    """name 超过 255 字符 → 400（对齐 VARCHAR(255)）。"""
    service = _make_service()
    client = TestClient(_make_app(service))

    resp = client.put(
        "/sessions/s1/name",
        json={"name": "长" * 256},
        headers=_auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.json()["code"] == 400
    service.rename_session.assert_not_awaited()


def test_rename_255_chars_accepted():
    """恰好 255 字符 → 放行。"""
    service = _make_service(ok=True)
    client = TestClient(_make_app(service))

    resp = client.put(
        "/sessions/s1/name",
        json={"name": "名" * 255},
        headers=_auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.json()["code"] == 200


def test_rename_session_not_found():
    """service 返回 False（会话不存在/不属于该用户）→ 404。"""
    service = _make_service(ok=False)
    client = TestClient(_make_app(service))

    resp = client.put(
        "/sessions/missing/name",
        json={"name": "新名字"},
        headers=_auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.json()["code"] == 404


def test_rename_401_without_jwt():
    client = TestClient(_make_app())
    resp = client.put("/sessions/s1/name", json={"name": "新名字"})
    assert resp.status_code == 401
