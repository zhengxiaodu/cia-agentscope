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
    def __init__(self, rows=None, rowcount=0, row=None, exec_errors=None):
        self.rows = rows or []  # fetchall 返回值
        self.row = row          # fetchone 返回值
        self.exec_errors = list(exec_errors or [])  # 按次抛出的异常
        self.rowcount = rowcount
        self.executed = []

    async def execute(self, sql, args=None):
        if self.exec_errors:
            raise self.exec_errors.pop(0)
        self.executed.append((sql, args))

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.row

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
    """INSERT...SELECT 单语句复制：SQL 形态与参数（favorite_id + title + user_id + pair ids）。"""
    cur = _FakeCursor(rowcount=2)
    dao = MessageFavoriteDAO(_FakePool(cur))

    favorite_id, copied = await dao.create_favorite("u1", ["p1", "p2"], "收藏标题")

    assert copied == 2
    assert len(favorite_id) == 16
    int(favorite_id, 16)
    sql, args = cur.executed[0]
    assert sql.startswith("INSERT INTO message_favorites")
    assert "(favorite_id, title, session_id, role, content, `timestamp`" in sql
    assert "SELECT %s, %s, m.session_id, m.role, m.content, m.`timestamp`" in sql
    assert "FROM messages m WHERE m.user_id = %s" in sql
    assert "AND m.message_pair_id IN (%s, %s)" in sql
    assert "ORDER BY m.id ASC" in sql
    assert args == [favorite_id, "收藏标题", "u1", "p1", "p2"]


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
    """timestamp strftime + JSON 列解析 + bool/int 归一化 + title 透传。"""
    cur = _FakeCursor(rows=[
        {
            "favorite_id": "f1",
            "title": "收藏标题",
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
            "title": None,  # 存量旧行 → 兜底空串
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
    assert r0["title"] == "收藏标题"
    assert r1["title"] == ""  # None → 空串
    assert r0["timestamp"] == "2026-09-15 10:00:00.123"
    assert r0["agent_ids"] == ["a1"]
    assert r0["success"] is True
    assert r1["success"] is False
    assert r1["citations"] == [{"doc_id": "d1"}]
    assert r1["bocha_sum"] == []
    sql, args = cur.executed[0]
    assert "SELECT favorite_id, title, session_id, role, content" in sql
    assert "WHERE user_id = %s ORDER BY id ASC" in sql
    assert args == ("u1",)


@pytest.mark.asyncio
async def test_dao_get_first_user_message_sql_and_args():
    """标题来源查询：WHERE 不带 session_id（与 create_favorite 对齐，支持跨会话）。"""
    cur = _FakeCursor(row={"content": "第一条用户提问"})
    dao = MessageFavoriteDAO(_FakePool(cur))

    content = await dao.get_first_user_message("u1", ["p1", "p2"])

    assert content == "第一条用户提问"
    sql, args = cur.executed[0]
    assert sql == (
        "SELECT content FROM messages "
        "WHERE user_id = %s "
        "AND message_pair_id IN (%s, %s) "
        "AND role = 'user' ORDER BY id ASC LIMIT 1"
    )
    assert args == ["u1", "p1", "p2"]


@pytest.mark.asyncio
async def test_dao_get_first_user_message_empty():
    """无匹配行返回空串；空 pair_ids 不发查询。"""
    cur = _FakeCursor(row=None)
    dao = MessageFavoriteDAO(_FakePool(cur))
    assert await dao.get_first_user_message("u1", ["p1"]) == ""
    # 空 pair_ids 直接返回，不发 SQL
    assert await dao.get_first_user_message("u1", []) == ""
    assert len(cur.executed) == 1


@pytest.mark.asyncio
async def test_dao_get_first_user_message_swallows_error():
    """查询异常静默返回空串，不阻断创建流程。"""
    cur = _FakeCursor(exec_errors=[RuntimeError("db down")])
    dao = MessageFavoriteDAO(_FakePool(cur))
    assert await dao.get_first_user_message("u1", ["p1"]) == ""


# ---------------------------------------------------------------------------
# 路由层
# ---------------------------------------------------------------------------


def _make_app(dao, session_dao=None, upload_file_dao=None) -> FastAPI:
    app = FastAPI()
    app.include_router(favorite_route.router)
    app.state.message_favorite_dao = dao
    app.state.session_dao = session_dao
    app.state.upload_file_dao = upload_file_dao
    return app


def _auth_headers() -> dict:
    token = create_access_token({"user_id": "u1", "department": "研发部"})
    return {"Authorization": f"Bearer {token}"}


def _make_dao():
    dao = MagicMock()
    dao.get_first_user_message = AsyncMock(return_value="问1")
    dao.create_favorite = AsyncMock(return_value=("fid0000000000001", 2))
    dao.delete_favorite = AsyncMock(return_value=2)
    dao.list_favorites = AsyncMock(return_value=[])
    return dao


def _make_session_daos(files_by_session=None, uploads_by_session=None):
    """构造 session_dao / upload_file_dao mock。

    load_session_files / list_files_by_session 按 session_id 从
    files_by_session / uploads_by_session 取返回值（缺省 []）。
    """
    files_by_session = files_by_session or {}
    uploads_by_session = uploads_by_session or {}

    session_dao = MagicMock()
    session_dao.load_session_files = AsyncMock(
        side_effect=lambda sid: files_by_session.get(sid, [])
    )
    upload_dao = MagicMock()
    upload_dao.list_files_by_session = AsyncMock(
        side_effect=lambda sid: uploads_by_session.get(sid, [])
    )
    return session_dao, upload_dao


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
    # 标题：先查组内首条用户消息，截断后随 pair_ids 一起传 DAO
    dao.get_first_user_message.assert_awaited_once_with("u1", ["p1", "p2"])
    dao.create_favorite.assert_awaited_once_with("u1", ["p1", "p2"], "问1")


def test_post_favorite_title_truncated_to_50():
    """标题 = 组内首条用户消息截断 50 字符（与会话名称生成逻辑一致）。"""
    dao = _make_dao()
    dao.get_first_user_message = AsyncMock(return_value="长" * 60)
    client = TestClient(_make_app(dao))

    resp = client.post(
        "/message_favorite", json={"message_pair_ids": ["p1"]},
        headers=_auth_headers(),
    )

    assert resp.json()["code"] == 200
    dao.create_favorite.assert_awaited_once_with("u1", ["p1"], "长" * 50)


def test_post_favorite_empty_rejected():
    dao = _make_dao()
    client = TestClient(_make_app(dao))

    for payload in ({"message_pair_ids": []}, {"message_pair_ids": [""]}, {}):
        resp = client.post("/message_favorite", json=payload, headers=_auth_headers())
        assert resp.json()["code"] == 400, payload

    dao.get_first_user_message.assert_not_awaited()
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


def _file(name, pair, **extra):
    return {
        "name": name, "path": name, "url": f"/files/x/{name}", "size": 1,
        "media_type": "text/markdown",
        "created_at": "2026-09-15 10:00:00.000",
        "message_id": 101, "message_pair_id": pair,
        **extra,
    }


def _upload(name, pair, **extra):
    return {
        "name": name, "size": 2, "media_type": "application/pdf",
        "created_at": "2026-09-15 09:59:00.000",
        "message_id": 100, "message_pair_id": pair,
        **extra,
    }


def test_get_favorites_grouped():
    """按 favorite_id 分组，组间保持收藏先后、组内保持消息顺序。"""
    dao = _make_dao()
    session_dao, upload_dao = _make_session_daos()

    def _msg(fid, role, content, pair, title=""):
        return {
            "favorite_id": fid, "title": title, "session_id": "s1",
            "role": role, "content": content,
            "timestamp": "2026-09-15 10:00:00.000",
            "agent_ids": [], "user_id": "u1", "success": True, "tokens": 1,
            "message_pair_id": pair, "citations": [], "bocha_sum": [],
        }

    dao.list_favorites = AsyncMock(return_value=[
        _msg("f1", "user", "问1", "p1"),
        _msg("f1", "assistant", "答1", "p1"),
        _msg("f2", "user", "问2", "p2"),
    ])
    client = TestClient(_make_app(dao, session_dao, upload_dao))

    resp = client.get("/message_favorites", headers=_auth_headers())

    assert resp.status_code == 200
    favorites = resp.json()["data"]["favorites"]
    assert [g["favorite_id"] for g in favorites] == ["f1", "f2"]
    assert [m["content"] for m in favorites[0]["messages"]] == ["问1", "答1"]
    assert favorites[1]["messages"][0]["message_pair_id"] == "p2"
    # 新增字段默认存在（无文件时为空列表）
    assert favorites[0]["files"] == []
    assert favorites[0]["upload_files"] == []
    assert favorites[1]["files"] == []
    assert favorites[1]["upload_files"] == []


def test_get_favorites_title_persisted_and_fallback():
    """title 持久化非空 → 直接展示；为空（存量旧数据）→ 组内首条 user 消息兜底。"""
    dao = _make_dao()

    def _msg(fid, role, content, pair, title=""):
        return {
            "favorite_id": fid, "title": title, "session_id": "s1",
            "role": role, "content": content,
            "timestamp": "2026-09-15 10:00:00.000",
            "agent_ids": [], "user_id": "u1", "success": True, "tokens": 1,
            "message_pair_id": pair, "citations": [], "bocha_sum": [],
        }

    dao.list_favorites = AsyncMock(return_value=[
        _msg("f1", "user", "问1", "p1", title="持久化标题"),
        _msg("f1", "assistant", "答1", "p1", title="持久化标题"),
        _msg("f2", "user", "问2", "p2"),          # title 为空 → 兜底
        _msg("f3", "assistant", "答3", "p3"),     # 无 user 消息 → 空串
    ])
    client = TestClient(_make_app(dao, *_make_session_daos()))

    resp = client.get("/message_favorites", headers=_auth_headers())

    assert resp.status_code == 200
    favorites = resp.json()["data"]["favorites"]
    by_fid = {g["favorite_id"]: g for g in favorites}
    assert by_fid["f1"]["title"] == "持久化标题"
    assert by_fid["f2"]["title"] == "问2"
    assert by_fid["f3"]["title"] == ""


def test_get_favorites_files_filtered_by_pair():
    """分组下 files/upload_files 仅含与组内 message_pair_id 关联的文件。"""
    dao = _make_dao()

    def _msg(fid, pair, sid="s1"):
        return {
            "favorite_id": fid, "session_id": sid, "role": "user",
            "content": "问", "timestamp": "2026-09-15 10:00:00.000",
            "agent_ids": [], "user_id": "u1", "success": True, "tokens": 1,
            "message_pair_id": pair, "citations": [], "bocha_sum": [],
        }

    dao.list_favorites = AsyncMock(return_value=[
        _msg("f1", "p1"), _msg("f1", "p1"), _msg("f2", "p2"),
    ])
    session_dao, upload_dao = _make_session_daos(
        files_by_session={
            "s1": [
                _file("a.md", "p1"),                    # f1 关联 → 保留
                _file("b.md", "p2"),                    # f2 关联，f1 剔除
                _file("c.md", None),                    # 未绑定 pair → 全部剔除
            ],
        },
        uploads_by_session={
            "s1": [
                _upload("up1.pdf", "p1"),               # f1 关联 → 保留
                _upload("up2.pdf", None),               # 未消费 → 全部剔除
            ],
        },
    )
    client = TestClient(_make_app(dao, session_dao, upload_dao))

    resp = client.get("/message_favorites", headers=_auth_headers())

    assert resp.status_code == 200
    favorites = resp.json()["data"]["favorites"]
    by_fid = {g["favorite_id"]: g for g in favorites}
    assert [f["name"] for f in by_fid["f1"]["files"]] == ["a.md"]
    assert [f["name"] for f in by_fid["f1"]["upload_files"]] == ["up1.pdf"]
    assert [f["name"] for f in by_fid["f2"]["files"]] == ["b.md"]
    assert by_fid["f2"]["upload_files"] == []
    # 文件字段与历史会话详情接口对齐
    f1_file = by_fid["f1"]["files"][0]
    assert set(f1_file.keys()) == {
        "name", "path", "url", "size", "media_type", "created_at",
        "message_id", "message_pair_id",
    }


def test_get_favorites_cross_session_matching():
    """跨会话收藏：文件按 (session_id, message_pair_id) 匹配，不跨会话错配。"""
    dao = _make_dao()

    def _msg(fid, pair, sid):
        return {
            "favorite_id": fid, "session_id": sid, "role": "user",
            "content": "问", "timestamp": "2026-09-15 10:00:00.000",
            "agent_ids": [], "user_id": "u1", "success": True, "tokens": 1,
            "message_pair_id": pair, "citations": [], "bocha_sum": [],
        }

    # 同一收藏组内两个 pair 来自不同会话；两会话存在同名 pair_id "px"
    dao.list_favorites = AsyncMock(return_value=[
        _msg("f1", "p1", "s1"),
        _msg("f1", "px", "s2"),
    ])
    session_dao, upload_dao = _make_session_daos(
        files_by_session={
            "s1": [_file("s1-file.md", "p1"), _file("s1-x.md", "px")],
            "s2": [_file("s2-file.md", "px")],
        },
    )
    client = TestClient(_make_app(dao, session_dao, upload_dao))

    resp = client.get("/message_favorites", headers=_auth_headers())

    assert resp.status_code == 200
    group = resp.json()["data"]["favorites"][0]
    # s1 的 px 文件不属于该组（组内 s1 只涉及 p1）；s2 的 px 属于该组
    assert [f["name"] for f in group["files"]] == ["s1-file.md", "s2-file.md"]
    # 每个 session 只查一次库（set 迭代顺序不定，按参数集合断言）
    awaited_sids = [
        c.args[0] for c in session_dao.load_session_files.await_args_list
    ]
    assert sorted(awaited_sids) == ["s1", "s2"]
    assert session_dao.load_session_files.await_count == 2


def test_get_favorites_upload_dao_error_tolerated():
    """upload_file_dao 查询异常 → 该会话 upload_files 降级为 []，不阻断。"""
    dao = _make_dao()
    dao.list_favorites = AsyncMock(return_value=[{
        "favorite_id": "f1", "session_id": "s1", "role": "user",
        "content": "问", "timestamp": "2026-09-15 10:00:00.000",
        "agent_ids": [], "user_id": "u1", "success": True, "tokens": 1,
        "message_pair_id": "p1", "citations": [], "bocha_sum": [],
    }])
    session_dao, upload_dao = _make_session_daos(
        files_by_session={"s1": [_file("a.md", "p1")]},
    )
    upload_dao.list_files_by_session = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    client = TestClient(_make_app(dao, session_dao, upload_dao))

    resp = client.get("/message_favorites", headers=_auth_headers())

    assert resp.status_code == 200
    group = resp.json()["data"]["favorites"][0]
    assert [f["name"] for f in group["files"]] == ["a.md"]
    assert group["upload_files"] == []


def test_get_favorites_empty():
    session_dao, upload_dao = _make_session_daos()
    client = TestClient(_make_app(_make_dao(), session_dao, upload_dao))

    resp = client.get("/message_favorites", headers=_auth_headers())

    assert resp.json()["data"] == {"favorites": []}
    # 无收藏时不触发文件查询
    session_dao.load_session_files.assert_not_awaited()
    upload_dao.list_files_by_session.assert_not_awaited()


def test_get_favorites_401_without_jwt():
    client = TestClient(_make_app(_make_dao()))
    resp = client.get("/message_favorites")
    assert resp.status_code == 401
