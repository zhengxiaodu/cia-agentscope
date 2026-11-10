"""消息分享测试：MessageShareDAO SQL/重试 + POST/GET 路由（含免登录与过滤逻辑）。

风格对齐 test_sessions_pagination.py：DAO 用假 pool/cursor 验证 SQL 与参数；
路由自建最小 app（真 JWT 签发 + mock DAO / session_service）。
"""
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.message_share_dao import MessageShareDAO
from app.models.session import SessionDetailResponse
from app.routes import message_share as share_route
from app.services.auth_service import create_access_token


# ---------------------------------------------------------------------------
# 假 pool / conn / cursor
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, row=None, exec_errors=None):
        self.row = row  # fetchone 返回值
        self.exec_errors = list(exec_errors or [])  # 按次抛出的异常
        self.executed = []
        self.rowcount = 0

    async def execute(self, sql, args=None):
        if self.exec_errors:
            raise self.exec_errors.pop(0)
        self.executed.append((sql, args))

    async def fetchone(self):
        return self.row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor_obj):
        self.cursor_obj = cursor_obj
        self.committed = 0

    def cursor(self, cursor_class=None):
        return self.cursor_obj

    async def commit(self):
        self.committed += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, cursor_obj):
        self.conn = _FakeConn(cursor_obj)

    def acquire(self):
        return self.conn


class _DupError(Exception):
    """模拟 MySQL 1062 唯一键冲突。"""

    def __init__(self):
        super().__init__(1062, "Duplicate entry")


# ---------------------------------------------------------------------------
# DAO 层
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dao_create_share_sql_and_args():
    cur = _FakeCursor()
    dao = MessageShareDAO(_FakePool(cur))

    shared_id = await dao.create_share("s1", "u1", ["p1", "p2"], "分享标题")

    assert len(shared_id) == 16
    int(shared_id, 16)  # 16 位十六进制
    sql, args = cur.executed[0]
    assert sql == (
        "INSERT INTO message_shares "
        "(shared_id, session_id, user_id, message_pair_ids, "
        "title) VALUES (%s, %s, %s, %s, %s)"
    )
    assert args == (shared_id, "s1", "u1", json.dumps(["p1", "p2"]), "分享标题")
    assert cur.executed[0][1] == (
        shared_id, "s1", "u1", '["p1", "p2"]', "分享标题",
    )


@pytest.mark.asyncio
async def test_dao_create_share_retries_on_duplicate():
    """首次 1062 冲突 → 换 id 重试成功。"""
    cur = _FakeCursor(exec_errors=[_DupError()])
    dao = MessageShareDAO(_FakePool(cur))

    shared_id = await dao.create_share("s1", "u1", ["p1"])

    assert len(shared_id) == 16
    assert len(cur.executed) == 1  # 第二次成功


@pytest.mark.asyncio
async def test_dao_create_share_duplicate_exhausted():
    """连续 3 次 1062 → 抛出最后异常。"""
    cur = _FakeCursor(exec_errors=[_DupError(), _DupError(), _DupError()])
    dao = MessageShareDAO(_FakePool(cur))

    with pytest.raises(Exception):
        await dao.create_share("s1", "u1", ["p1"])


@pytest.mark.asyncio
async def test_dao_get_share_parses_json():
    cur = _FakeCursor(row={
        "shared_id": "abc123def4567890",
        "session_id": "s1",
        "user_id": "u1",
        "message_pair_ids": json.dumps(["p1", "p2"]),
        "title": "分享标题",
        "created_at": datetime(2026, 9, 15, 10, 0, 0),
    })
    dao = MessageShareDAO(_FakePool(cur))

    share = await dao.get_share("abc123def4567890")

    assert share["message_pair_ids"] == ["p1", "p2"]
    assert share["session_id"] == "s1"
    assert share["user_id"] == "u1"
    assert share["title"] == "分享标题"


@pytest.mark.asyncio
async def test_dao_get_first_user_message_sql_and_args():
    cur = _FakeCursor(row={"content": "第一条用户提问"})
    dao = MessageShareDAO(_FakePool(cur))

    content = await dao.get_first_user_message("u1", "s1", ["p1", "p2"])

    assert content == "第一条用户提问"
    sql, args = cur.executed[0]
    assert sql == (
        "SELECT content FROM messages "
        "WHERE user_id = %s AND session_id = %s "
        "AND message_pair_id IN (%s, %s) "
        "AND role = 'user' ORDER BY id ASC LIMIT 1"
    )
    assert args == ["u1", "s1", "p1", "p2"]


@pytest.mark.asyncio
async def test_dao_get_first_user_message_empty():
    """无匹配行返回空串；空 pair_ids 不发查询。"""
    cur = _FakeCursor(row=None)
    dao = MessageShareDAO(_FakePool(cur))
    assert await dao.get_first_user_message("u1", "s1", ["p1"]) == ""
    # 空 pair_ids 直接返回，不发 SQL
    assert await dao.get_first_user_message("u1", "s1", []) == ""
    assert len(cur.executed) == 1


@pytest.mark.asyncio
async def test_dao_get_first_user_message_swallows_error():
    """查询异常静默返回空串，不阻断创建流程。"""
    cur = _FakeCursor(exec_errors=[RuntimeError("db down")])
    dao = MessageShareDAO(_FakePool(cur))
    assert await dao.get_first_user_message("u1", "s1", ["p1"]) == ""


@pytest.mark.asyncio
async def test_dao_get_share_not_found():
    dao = MessageShareDAO(_FakePool(_FakeCursor(row=None)))
    assert await dao.get_share("missing") is None


# ---------------------------------------------------------------------------
# 路由层
# ---------------------------------------------------------------------------


def _make_detail() -> SessionDetailResponse:
    return SessionDetailResponse(
        session_id="s1",
        created_at="2026-09-01 10:00:00.000",
        updated_at="2026-09-02 10:00:00.000",
        trace_id="trace-1",
        messages=[
            {"role": "user", "content": "问1", "timestamp": "t",
             "message_pair_id": "p1"},
            {"role": "assistant", "content": "答1", "timestamp": "t",
             "message_pair_id": "p1"},
            {"role": "user", "content": "问2", "timestamp": "t",
             "message_pair_id": "p2"},
        ],
        files=[
            {"name": "a.md", "path": "a.md", "url": "/files/a.md", "size": 1,
             "media_type": "text/markdown", "message_pair_id": "p1"},
            {"name": "b.md", "path": "b.md", "url": "/files/b.md", "size": 2,
             "media_type": "text/markdown", "message_pair_id": "p2"},
        ],
        upload_files=[
            {"name": "up.pdf", "size": 3, "media_type": "application/pdf",
             "message_pair_id": "p2"},
        ],
    )


def _make_app(share_dao=None, session_dao=None, session_service=None) -> FastAPI:
    app = FastAPI()
    app.include_router(share_route.router)
    app.state.message_share_dao = share_dao
    app.state.session_dao = session_dao
    app.state.session_service = session_service
    return app


def _auth_headers() -> dict:
    token = create_access_token({"user_id": "u1", "department": "研发部"})
    return {"Authorization": f"Bearer {token}"}


def _make_share_dao(shared_id="abc123def4567890"):
    dao = MagicMock()
    dao.create_share = AsyncMock(return_value=shared_id)
    dao.get_first_user_message = AsyncMock(return_value="问1")
    dao.get_share = AsyncMock(return_value={
        "shared_id": shared_id,
        "session_id": "s1",
        "user_id": "u1",
        "message_pair_ids": ["p1"],
        "title": "",  # 存量旧数据：空标题 → 详情接口动态兜底
        "created_at": datetime(2026, 9, 15, 10, 0, 0),
    })
    return dao


def _make_session_dao(meta=None):
    dao = MagicMock()
    dao.get_session_meta = AsyncMock(return_value=meta)
    return dao


def _make_session_service(detail=_make_detail()):
    service = MagicMock()
    service.get_session_detail = AsyncMock(return_value=detail)
    return service


# ---- POST /message_share ----


def test_post_share_success():
    share_dao = _make_share_dao()
    session_dao = _make_session_dao(meta={"user_id": "u1"})
    client = TestClient(_make_app(share_dao, session_dao))

    resp = client.post(
        "/message_share",
        json={"session_id": "s1", "message_pair_ids": ["p1", "p1", " p2 "]},
        headers=_auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["data"]["shared_id"] == "abc123def4567890"
    # strip + 去重保序后透传给 DAO（DAO 签名：session_id, user_id, pair_ids, title）
    share_dao.get_first_user_message.assert_awaited_once_with("u1", "s1", ["p1", "p2"])
    share_dao.create_share.assert_awaited_once_with("s1", "u1", ["p1", "p2"], "问1")


def test_post_share_title_truncated_to_50():
    """标题 = 首条用户消息截断 50 字符（与会话名称生成逻辑一致）。"""
    share_dao = _make_share_dao()
    share_dao.get_first_user_message = AsyncMock(return_value="长" * 60)
    client = TestClient(_make_app(share_dao, _make_session_dao({"user_id": "u1"})))

    resp = client.post(
        "/message_share",
        json={"session_id": "s1", "message_pair_ids": ["p1"]},
        headers=_auth_headers(),
    )

    assert resp.json()["code"] == 200
    share_dao.create_share.assert_awaited_once_with("s1", "u1", ["p1"], "长" * 50)


def test_post_share_empty_pair_ids_rejected():
    client = TestClient(_make_app(_make_share_dao(), _make_session_dao({"user_id": "u1"})))
    for payload in (
        {"session_id": "s1", "message_pair_ids": []},
        {"session_id": "s1", "message_pair_ids": ["  "]},
        {"session_id": "s1"},           # 缺失
        {"session_id": "", "message_pair_ids": ["p1"]},  # session_id 为空
    ):
        resp = client.post("/message_share", json=payload, headers=_auth_headers())
        assert resp.status_code == 200, payload
        assert resp.json()["code"] == 400, payload


def test_post_share_session_not_found():
    client = TestClient(_make_app(_make_share_dao(), _make_session_dao(meta=None)))

    resp = client.post(
        "/message_share",
        json={"session_id": "missing", "message_pair_ids": ["p1"]},
        headers=_auth_headers(),
    )

    assert resp.json()["code"] == 404


def test_post_share_not_owner_forbidden():
    client = TestClient(_make_app(_make_share_dao(), _make_session_dao(meta={"user_id": "other"})))

    resp = client.post(
        "/message_share",
        json={"session_id": "s1", "message_pair_ids": ["p1"]},
        headers=_auth_headers(),
    )

    assert resp.json()["code"] == 403


def test_post_share_401_without_jwt():
    client = TestClient(_make_app(_make_share_dao(), _make_session_dao()))
    resp = client.post(
        "/message_share", json={"session_id": "s1", "message_pair_ids": ["p1"]},
    )
    assert resp.status_code == 401


# ---- GET /message_share/{shared_id} ----


def test_get_share_no_auth_required_and_filters():
    """免登录访问；messages/files/upload_files 均按 message_pair_id 过滤。"""
    share_dao = _make_share_dao()
    session_service = _make_session_service()
    client = TestClient(_make_app(share_dao, session_service=session_service))

    resp = client.get("/message_share/abc123def4567890")  # 不带 JWT

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["shared_id"] == "abc123def4567890"
    # 分享的 pair 是 p1：仅保留 p1 的消息/文件
    assert [m["content"] for m in data["messages"]] == ["问1", "答1"]
    assert [f["name"] for f in data["files"]] == ["a.md"]
    assert data["upload_files"] == []  # p2 的上传文件被剔除
    # title 持久化为空（存量）→ 从被分享消息兜底取首条 user 消息
    assert data["title"] == "问1"
    # 用分享者 user_id 调详情（通过属主校验）
    session_service.get_session_detail.assert_awaited_once_with("s1", "u1")


def test_get_share_title_persisted():
    """创建时持久化的 title 非空 → 直接展示，不做动态兜底。"""
    share_dao = _make_share_dao()
    share_dao.get_share = AsyncMock(return_value={
        "shared_id": "abc123def4567890",
        "session_id": "s1",
        "user_id": "u1",
        "message_pair_ids": ["p1"],
        "title": "持久化的分享标题",
        "created_at": datetime(2026, 9, 15, 10, 0, 0),
    })
    client = TestClient(_make_app(share_dao, session_service=_make_session_service()))

    resp = client.get("/message_share/abc123def4567890")

    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "持久化的分享标题"


def test_get_share_not_found():
    share_dao = _make_share_dao()
    share_dao.get_share = AsyncMock(return_value=None)
    client = TestClient(
        _make_app(share_dao, session_service=_make_session_service())
    )

    resp = client.get("/message_share/missing")

    assert resp.json()["code"] == 404


def test_get_share_session_deleted():
    share_dao = _make_share_dao()
    session_service = _make_session_service(detail=None)
    client = TestClient(_make_app(share_dao, session_service=session_service))

    resp = client.get("/message_share/abc123def4567890")

    assert resp.json()["code"] == 404
