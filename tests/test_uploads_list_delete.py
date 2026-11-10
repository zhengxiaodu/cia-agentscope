"""/uploads 列表补 upload_file_id + DELETE /uploads 删除接口测试。"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.upload_file_dao import UploadFileDAO
from app.routes import upload as upload_route
from app.services.auth_service import create_access_token


# ---------------------------------------------------------------------------
# 假 pool / conn / cursor
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows=None, rowcount=0):
        self.rows = rows or []
        self.rowcount = rowcount
        self.executed = []

    async def execute(self, sql, args=None):
        self.executed.append((sql, args))

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor_obj):
        self.cursor_obj = cursor_obj

    def cursor(self, cursor_class=None):
        return self.cursor_obj

    async def commit(self):
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
# DAO 层
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dao_list_files_by_user_returns_upload_file_id():
    cur = _FakeCursor(rows=[
        {"id": 3, "session_id": "s1", "message_id": 101,
         "filename": "a.pdf", "media_type": "application/pdf", "file_size": 10},
        {"id": 2, "session_id": "s2", "message_id": None,
         "filename": "b.pdf", "media_type": "application/pdf", "file_size": 20},
    ])
    dao = UploadFileDAO(_FakePool(cur))

    files = await dao.list_files_by_user("u1")

    assert files == [
        {"upload_file_id": 3, "session_id": "s1", "message_id": 101,
         "filename": "a.pdf", "media_type": "application/pdf", "file_size": 10},
        {"upload_file_id": 2, "session_id": "s2", "message_id": None,
         "filename": "b.pdf", "media_type": "application/pdf", "file_size": 20},
    ]
    sql, args = cur.executed[0]
    assert "SELECT id, session_id, message_id, filename, media_type, file_size" in sql
    assert "ORDER BY id DESC" in sql
    assert args == ("u1",)


@pytest.mark.asyncio
async def test_dao_delete_by_id():
    cur = _FakeCursor(rowcount=1)
    dao = UploadFileDAO(_FakePool(cur))

    ok = await dao.delete_by_id("u1", 5)

    assert ok is True
    sql, args = cur.executed[0]
    assert sql == "DELETE FROM upload_files WHERE id = %s AND user_id = %s"
    assert args == (5, "u1")


@pytest.mark.asyncio
async def test_dao_delete_by_id_not_found():
    dao = UploadFileDAO(_FakePool(_FakeCursor(rowcount=0)))
    assert await dao.delete_by_id("u1", 99) is False


@pytest.mark.asyncio
async def test_dao_delete_all_by_user():
    cur = _FakeCursor(rowcount=7)
    dao = UploadFileDAO(_FakePool(cur))

    deleted = await dao.delete_all_by_user("u1")

    assert deleted == 7
    sql, args = cur.executed[0]
    assert sql == "DELETE FROM upload_files WHERE user_id = %s"
    assert args == ("u1",)


# ---------------------------------------------------------------------------
# 路由层：GET /uploads（补字段）+ DELETE /uploads
# ---------------------------------------------------------------------------


def _make_app(dao) -> FastAPI:
    app = FastAPI()
    app.include_router(upload_route.router)
    app.state.upload_file_dao = dao
    return app


def _auth_headers() -> dict:
    token = create_access_token({"user_id": "u1", "department": "研发部"})
    return {"Authorization": f"Bearer {token}"}


def _make_dao(files=None, delete_by_id=True, delete_all=0):
    dao = MagicMock()
    dao.list_files_by_user = AsyncMock(return_value=files or [])
    dao.delete_by_id = AsyncMock(return_value=delete_by_id)
    dao.delete_all_by_user = AsyncMock(return_value=delete_all)
    return dao


def test_uploads_list_contains_upload_file_id():
    files = [{"upload_file_id": 3, "session_id": "s1", "message_id": None,
              "filename": "a.pdf", "media_type": "application/pdf",
              "file_size": 10}]
    dao = _make_dao(files=files)
    client = TestClient(_make_app(dao))

    resp = client.get("/uploads", headers=_auth_headers())

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["files"][0]["upload_file_id"] == 3
    dao.list_files_by_user.assert_awaited_once_with("u1")


def test_delete_uploads_by_id_success():
    dao = _make_dao(delete_by_id=True)
    client = TestClient(_make_app(dao))

    resp = client.delete("/uploads", params={"upload_file_id": 5},
                         headers=_auth_headers())

    assert resp.status_code == 200
    assert resp.json()["data"] == {"deleted": 1}
    dao.delete_by_id.assert_awaited_once_with("u1", 5)
    dao.delete_all_by_user.assert_not_awaited()


def test_delete_uploads_by_id_not_found():
    dao = _make_dao(delete_by_id=False)
    client = TestClient(_make_app(dao))

    resp = client.delete("/uploads", params={"upload_file_id": 99},
                         headers=_auth_headers())

    assert resp.status_code == 200
    assert resp.json()["code"] == 404


def test_delete_uploads_all_when_id_absent():
    dao = _make_dao(delete_all=7)
    client = TestClient(_make_app(dao))

    resp = client.delete("/uploads", headers=_auth_headers())

    assert resp.status_code == 200
    assert resp.json()["data"] == {"deleted": 7}
    dao.delete_all_by_user.assert_awaited_once_with("u1")
    dao.delete_by_id.assert_not_awaited()


def test_delete_uploads_401_without_jwt():
    client = TestClient(_make_app(_make_dao()))
    resp = client.delete("/uploads")
    assert resp.status_code == 401
