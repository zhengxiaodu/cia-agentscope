"""收藏消息测试：MessageFavoriteDAO（INSERT...SELECT/删除/列表格式化）+ 三个路由。"""
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.message_favorite_dao import MessageFavoriteDAO
from app.routes import message_favorite as favorite_route
from app.services.auth_service import create_access_token


# ---------------------------------------------------------------------------
# 假 pool / conn / cursor
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows=None, rowcount=0):
        self.rows = rows or []  # fetchall 返回值
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
async def test_dao_create_favorite_insert_select():
    """INSERT...SELECT 单语句复制：SQL 形态与参数（favorite_id + user_id + pair ids）。"""
    cur = _FakeCursor(rowcount=2)
    dao = MessageFavoriteDAO(_FakePool(cur))

    favorite_id, copied = await dao.create_favorite("u1", ["p1", "p2"])

    assert copied == 2
    assert len(favorite_id) == 16
    int(favorite_id, 16)
    sql, args = cur.executed[0]
    assert sql.startswith("INSERT INTO message_favorites")
    assert "SELECT %s, m.session_id, m.role, m.content, m.`timestamp`" in sql
    assert "FROM messages m WHERE m.user_id = %s" in sql
    assert "AND m.message_pair_id IN (%s, %s)" in sql
    assert "ORDER BY m.id ASC" in sql
    assert args == [favorite_id, "u1", "p1", "p2"]


@pytest.mark.asyncio
async def test_dao_create_favorite_single_pair():
    cur = _FakeCursor(rowcount=0)
    dao = MessageFavoriteDAO(_FakePool(cur))

    _, copied = await dao.create_favorite("u1", ["p1"])

    assert copied == 0
    assert "IN (%s)" in cur.executed[0][0]


@pytest.mark.asyncio
async def test_dao_delete_favorite():
    cur = _FakeCursor(rowcount=3)
    dao = MessageFavoriteDAO(_FakePool(cur))

    deleted = await dao.delete_favorite("u1", "fid123")

    assert deleted == 3
    sql, args = cur.executed[0]
    assert sql == (
        "DELETE FROM message_favorites "
        "WHERE user_id = %s AND favorite_id = %s"
    )
    assert args == ("u1", "fid123")


@pytest.mark.asyncio
async def test_dao_list_favorites_formats_rows():
    """timestamp strftime + JSON 列解析 + bool/int 归一化。"""
    cur = _FakeCursor(rows=[
        {
            "favorite_id": "f1",
            "session_id": "s1",
            "role": "user",
            "content": "问1",
            "timestamp": datetime(2026, 9, 15, 10, 0, 0, 123456),
            "agent_ids": json.dumps(["a1"]),
            "user_id": "u1",
            "success": 1,
            "tokens": 12,
            "message_pair_id": "p1",
            "citations": None,
            "bocha_sum": None,
        },
        {
            "favorite_id": "f1",
            "session_id": "s1",
            "role": "assistant",
            "content": "答1",
            "timestamp": datetime(2026, 9, 15, 10, 0, 5, 654321),
            "agent_ids": None,
            "user_id": "u1",
            "success": 0,
            "tokens": 34,
            "message_pair_id": "p1",
            "citations": json.dumps([{"doc_id": "d1"}]),
            "bocha_sum": None,
        },
    ])
    dao = MessageFavoriteDAO(_FakePool(cur))

    rows = await dao.list_favorites("u1")

    assert len(rows) == 2
    r0, r1 = rows
    assert r0["timestamp"] == "2026-09-15 10:00:00.123"
    assert r0["agent_ids"] == ["a1"]
    assert r0["success"] is True
    assert r1["success"] is False
    assert r1["citations"] == [{"doc_id": "d1"}]
    assert r1["bocha_sum"] == []
    sql, args = cur.executed[0]
    assert "WHERE user_id = %s ORDER BY id ASC" in sql
    assert args == ("u1",)


# ---------------------------------------------------------------------------
# 路由层
# ---------------------------------------------------------------------------


def _make_app(dao) -> FastAPI:
    app = FastAPI()
    app.include_router(favorite_route.router)
    app.state.message_favorite_dao = dao
    return app


def _auth_headers() -> dict:
    token = create_access_token({"user_id": "u1", "department": "研发部"})
    return {"Authorization": f"Bearer {token}"}


def _make_dao():
    dao = MagicMock()
    dao.create_favorite = AsyncMock(return_value=("fid0000000000001", 2))
    dao.delete_favorite = AsyncMock(return_value=2)
    dao.list_favorites = AsyncMock(return_value=[])
    return dao


def test_post_favorite_success():
    dao = _make_dao()
    client = TestClient(_make_app(dao))

    resp = client.post(
        "/message_favorite",
        json={"message_pair_ids": ["p1", "p1", " p2 "]},
        headers=_auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["data"] == {"favorite_id": "fid0000000000001", "count": 2}
    dao.create_favorite.assert_awaited_once_with("u1", ["p1", "p2"])


def test_post_favorite_empty_rejected():
    dao = _make_dao()
    client = TestClient(_make_app(dao))

    for payload in ({"message_pair_ids": []}, {"message_pair_ids": [""]}, {}):
        resp = client.post("/message_favorite", json=payload, headers=_auth_headers())
        assert resp.json()["code"] == 400, payload

    dao.create_favorite.assert_not_awaited()


def test_post_favorite_no_messages_found():
    dao = _make_dao()
    dao.create_favorite = AsyncMock(return_value=("fid0000000000001", 0))
    client = TestClient(_make_app(dao))

    resp = client.post(
        "/message_favorite", json={"message_pair_ids": ["missing"]},
        headers=_auth_headers(),
    )

    assert resp.json()["code"] == 404


def test_post_favorite_401_without_jwt():
    client = TestClient(_make_app(_make_dao()))
    resp = client.post("/message_favorite", json={"message_pair_ids": ["p1"]})
    assert resp.status_code == 401


def test_delete_favorite_success():
    dao = _make_dao()
    client = TestClient(_make_app(dao))

    resp = client.delete("/message_favorite/fid0000000000001", headers=_auth_headers())

    assert resp.json()["data"] == {"deleted": 2}
    dao.delete_favorite.assert_awaited_once_with("u1", "fid0000000000001")


def test_delete_favorite_not_found():
    dao = _make_dao()
    dao.delete_favorite = AsyncMock(return_value=0)
    client = TestClient(_make_app(dao))

    resp = client.delete("/message_favorite/missing", headers=_auth_headers())

    assert resp.json()["code"] == 404


def test_get_favorites_grouped():
    """按 favorite_id 分组，组间保持收藏先后、组内保持消息顺序。"""
    dao = _make_dao()

    def _msg(fid, role, content, pair):
        return {
            "favorite_id": fid, "session_id": "s1", "role": role,
            "content": content, "timestamp": "2026-09-15 10:00:00.000",
            "agent_ids": [], "user_id": "u1", "success": True, "tokens": 1,
            "message_pair_id": pair, "citations": [], "bocha_sum": [],
        }

    dao.list_favorites = AsyncMock(return_value=[
        _msg("f1", "user", "问1", "p1"),
        _msg("f1", "assistant", "答1", "p1"),
        _msg("f2", "user", "问2", "p2"),
    ])
    client = TestClient(_make_app(dao))

    resp = client.get("/message_favorites", headers=_auth_headers())

    assert resp.status_code == 200
    favorites = resp.json()["data"]["favorites"]
    assert [g["favorite_id"] for g in favorites] == ["f1", "f2"]
    assert [m["content"] for m in favorites[0]["messages"]] == ["问1", "答1"]
    assert favorites[1]["messages"][0]["message_pair_id"] == "p2"


def test_get_favorites_empty():
    client = TestClient(_make_app(_make_dao()))

    resp = client.get("/message_favorites", headers=_auth_headers())

    assert resp.json()["data"] == {"favorites": []}


def test_get_favorites_401_without_jwt():
    client = TestClient(_make_app(_make_dao()))
    resp = client.get("/message_favorites")
    assert resp.status_code == 401
