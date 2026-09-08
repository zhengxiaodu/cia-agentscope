"""GET /uploads 按 user_id 查询上传文件的测试：DAO SQL 路径 + 路由鉴权与响应结构。

无现成 FastAPI TestClient 基建，本文件自建最小 app（仅挂被测路由 + 真 JWT 签发），
DAO 用假 pool/cursor 验证 SQL 与参数化查询（风格对齐 test_regulations_knowledge_gap）。
"""
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from app.dao.upload_file_dao import UploadFileDAO
from app.routes import upload as upload_route
from app.services.auth_service import create_access_token


# ---------------------------------------------------------------------------
# DAO：假 pool / conn / cursor
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []
        self.lastrowid = None

    async def execute(self, sql, args=None):
        self.executed.append((sql, args))

    async def fetchall(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)
        self.committed = False

    def cursor(self, cursor_class=None):
        return self.cursor_obj

    async def begin(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, rows):
        self._rows = rows
        self.conn = _FakeConn(rows)

    def acquire(self):
        return self.conn


@pytest.mark.asyncio
async def test_list_files_by_user_sql_and_mapping():
    rows = [
        {"session_id": "s2", "message_id": None, "filename": "b.pdf",
         "media_type": "application/pdf", "file_size": 2048},
        {"session_id": "s1", "message_id": 7, "filename": "a.m4a",
         "media_type": "audio/mp4", "file_size": 1024},
    ]
    pool = _FakePool(rows)
    dao = UploadFileDAO(pool)

    result = await dao.list_files_by_user("u1")

    sql, args = pool.conn.cursor_obj.executed[0]
    assert "FROM upload_files WHERE user_id = %s" in sql
    assert "ORDER BY id DESC" in sql
    assert args == ("u1",)
    assert result == rows
    assert pool.conn.committed


@pytest.mark.asyncio
async def test_list_files_by_user_empty():
    pool = _FakePool([])
    dao = UploadFileDAO(pool)
    assert await dao.list_files_by_user("nobody") == []


@pytest.mark.asyncio
async def test_insert_writes_user_id_and_file_size():
    pool = _FakePool([])
    dao = UploadFileDAO(pool)
    pool.conn.cursor_obj.lastrowid = 42

    file_id = await dao.insert("s1", "u1", "a.pdf", "application/pdf", "mineru", 123)

    assert file_id == 42
    sql, args = pool.conn.cursor_obj.executed[0]
    assert "user_id" in sql and "file_size" in sql
    assert args == ("s1", "u1", "a.pdf", "application/pdf", "mineru", 123)


# ---------------------------------------------------------------------------
# 路由：GET /uploads
# ---------------------------------------------------------------------------

def _make_app(rows=None, dao=None) -> FastAPI:
    app = FastAPI()
    app.include_router(upload_route.router)
    if dao is None:
        dao = MagicMock()
        dao.list_files_by_user = AsyncMock(return_value=rows or [])
    app.state.upload_file_dao = dao
    return app


def _auth_headers(payload=None) -> dict:
    token = create_access_token(payload or {"user_id": "u1", "department": "研发部"})
    return {"Authorization": f"Bearer {token}"}


def test_uploads_401_without_jwt():
    client = TestClient(_make_app())
    resp = client.get("/uploads", params={"user_id": "u1"})
    assert resp.status_code == 401


def test_uploads_422_when_user_id_missing():
    client = TestClient(_make_app())
    resp = client.get("/uploads", headers=_auth_headers())
    assert resp.status_code == 422


def test_uploads_200_returns_file_list():
    rows = [
        {"session_id": "s2", "message_id": None, "filename": "b.pdf",
         "media_type": "application/pdf", "file_size": 2048},
        {"session_id": "s1", "message_id": 7, "filename": "a.m4a",
         "media_type": "audio/mp4", "file_size": 1024},
    ]
    dao = MagicMock()
    dao.list_files_by_user = AsyncMock(return_value=rows)
    client = TestClient(_make_app(dao=dao))

    resp = client.get("/uploads", params={"user_id": "u9"}, headers=_auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["msg"] == "success"
    assert body["data"]["files"] == rows
    # 每条记录契约字段齐全（message_id 未绑定为 None）
    for item in body["data"]["files"]:
        assert set(item.keys()) == {
            "session_id", "message_id", "filename", "media_type", "file_size"
        }
    dao.list_files_by_user.assert_awaited_once_with("u9")


def test_uploads_500_when_dao_missing():
    app = _make_app()
    app.state.upload_file_dao = None
    client = TestClient(app)
    resp = client.get("/uploads", params={"user_id": "u1"}, headers=_auth_headers())
    assert resp.status_code == 500
