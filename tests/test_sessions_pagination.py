"""GET /sessions 会话列表分页测试：DAO 分页 SQL + 路由参数校验与 pagination 元数据。

风格对齐 test_upload_query.py：DAO 用假 pool/cursor 验证 SQL 与参数；
路由自建最小 app（真 JWT 签发 + mock session_service）。
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.mysql_session_dao import SessionDAO
from app.models.session import SessionMeta
from app.routes import sessions as sessions_route
from app.services.auth_service import create_access_token


# ---------------------------------------------------------------------------
# DAO：假 pool / conn / cursor（按最后一条 SQL 返回不同结果）
# ---------------------------------------------------------------------------


def _row(sid, pinned=False):
    """构造 sessions 表行（datetime 字段供 strftime）。"""
    return {
        "session_id": sid,
        "user_id": "u1",
        "name": f"会话{sid}",
        "created_at": datetime(2026, 9, 1, 12, 0, 0),
        "updated_at": datetime(2026, 9, 2, 12, 0, 0),
        "message_count": 2,
        "latest_trace_id": f"trace-{sid}",
        "is_pinned": 1 if pinned else 0,
        "agent_ids": None,
    }


class _FakeCursor:
    """按最后一条 SQL 特征路由 fetchall/fetchone 结果。

    - pinned 查询（is_pinned = 1）→ pinned_rows
    - COUNT 查询 → fetchone 返回 {"cnt": total}
    - 分页查询（is_pinned = 0 ... OFFSET）→ page_rows
    """

    def __init__(self, pinned_rows, page_rows, total):
        self._pinned_rows = pinned_rows
        self._page_rows = page_rows
        self._total = total
        self.executed = []
        self._last_sql = ""

    async def execute(self, sql, args=None):
        self.executed.append((sql, args))
        self._last_sql = sql

    async def fetchall(self):
        if "is_pinned = 1" in self._last_sql:
            return self._pinned_rows
        return self._page_rows

    async def fetchone(self):
        return {"cnt": self._total}

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


def _make_dao(pinned_rows=None, page_rows=None, total=0):
    cur = _FakeCursor(pinned_rows or [], page_rows or [], total)
    dao = SessionDAO(_FakePool(cur))
    return dao, cur


# ---------------------------------------------------------------------------
# DAO 层测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dao_default_page_params_and_sql():
    """默认 page=1/page_size=15：三条 SQL（置顶/COUNT/分页）形态与参数正确。"""
    dao, cur = _make_dao(
        pinned_rows=[_row("p1", pinned=True)],
        page_rows=[_row("s1"), _row("s2")],
        total=42,
    )

    top, sessions, total = await dao.list_user_sessions("u1")

    assert len(cur.executed) == 3
    # 1. 置顶查询：LIMIT 5，无 OFFSET
    sql0, args0 = cur.executed[0]
    assert "is_pinned = 1" in sql0
    assert "LIMIT %s" in sql0
    assert args0 == ("u1", 5)
    # 2. COUNT 查询
    sql1, args1 = cur.executed[1]
    assert "COUNT(*) AS cnt" in sql1
    assert args1 == ("u1",)
    # 3. 分页查询：LIMIT + OFFSET，offset=0
    sql2, args2 = cur.executed[2]
    assert "is_pinned = 0" in sql2
    assert "ORDER BY updated_at DESC" in sql2
    assert "LIMIT %s OFFSET %s" in sql2
    assert args2 == ("u1", 15, 0)

    # 返回三元组：置顶映射 + 当前页映射 + 总数
    assert top[0]["session_id"] == "p1"
    assert top[0]["is_pinned"] is True
    assert [s["session_id"] for s in sessions] == ["s1", "s2"]
    assert sessions[0]["created_at"] == "2026-09-01 12:00:00.000"
    assert total == 42


@pytest.mark.asyncio
async def test_dao_page2_offset_computed():
    """page=2/page_size=10 → OFFSET 10；置顶查询不受分页影响。"""
    dao, cur = _make_dao(total=25)

    await dao.list_user_sessions("u1", page=2, page_size=10)

    sql2, args2 = cur.executed[2]
    assert args2 == ("u1", 10, 10)
    # 置顶查询仍是 LIMIT 5
    assert cur.executed[0][1] == ("u1", 5)


@pytest.mark.asyncio
async def test_dao_empty_result():
    """无置顶、无会话、total=0 → 两个空列表 + 0。"""
    dao, _ = _make_dao(pinned_rows=[], page_rows=[], total=0)

    top, sessions, total = await dao.list_user_sessions("nobody")

    assert top == []
    assert sessions == []
    assert total == 0


# ---------------------------------------------------------------------------
# 路由层：GET /sessions
# ---------------------------------------------------------------------------


def _meta(sid, pinned=False):
    return SessionMeta(
        session_id=sid, user_id="u1", name=f"会话{sid}",
        created_at="2026-09-01 12:00:00.000",
        updated_at="2026-09-02 12:00:00.000",
        message_count=2, agent_ids=[],
    )


def _make_app(service=None) -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_route.router)
    if service is None:
        service = MagicMock()
        service.list_user_sessions = AsyncMock(return_value=([], [], 0))
    app.state.session_service = service
    return app


def _auth_headers() -> dict:
    token = create_access_token({"user_id": "u1", "department": "研发部"})
    return {"Authorization": f"Bearer {token}"}


def _make_service(top=None, sessions=None, total=0):
    service = MagicMock()
    service.list_user_sessions = AsyncMock(
        return_value=(top or [], sessions or [], total)
    )
    return service


def test_sessions_default_params_backward_compatible():
    """不传参数 → 默认 page=1 / page_size=15（与旧行为一致）+ pagination 字段。"""
    service = _make_service(total=15)
    client = TestClient(_make_app(service))

    resp = client.get("/sessions", headers=_auth_headers())

    assert resp.status_code == 200
    service.list_user_sessions.assert_awaited_once_with("u1", page=1, page_size=15)
    body = resp.json()
    assert body["code"] == 200
    assert body["data"]["top_sessions"] == []
    assert body["data"]["sessions"] == []
    assert body["data"]["pagination"] == {
        "page": 1, "page_size": 15, "total": 15,
        "total_pages": 1, "has_more": False,
    }


def test_sessions_custom_page_params_forwarded():
    """?page=2&page_size=10 → 透传给 service；offset 语义由 DAO 保证。"""
    service = _make_service(total=25)
    client = TestClient(_make_app(service))

    resp = client.get(
        "/sessions", params={"page": 2, "page_size": 10}, headers=_auth_headers(),
    )

    assert resp.status_code == 200
    service.list_user_sessions.assert_awaited_once_with("u1", page=2, page_size=10)
    # 25 条 / page_size=10 → total_pages=3，page=2 < 3 → has_more=True
    assert resp.json()["data"]["pagination"] == {
        "page": 2, "page_size": 10, "total": 25,
        "total_pages": 3, "has_more": True,
    }


def test_sessions_pagination_metadata_correctness():
    """total=42/page_size=15 → total_pages=3；page=1 has_more，page=3 收尾。"""
    # 第 1 页
    service = _make_service(total=42)
    client = TestClient(_make_app(service))
    resp = client.get(
        "/sessions", params={"page": 1, "page_size": 15}, headers=_auth_headers(),
    )
    p = resp.json()["data"]["pagination"]
    assert p["total_pages"] == 3 and p["has_more"] is True

    # 第 3 页（最后一页）
    service3 = _make_service(total=42)
    client3 = TestClient(_make_app(service3))
    resp3 = client3.get(
        "/sessions", params={"page": 3, "page_size": 15}, headers=_auth_headers(),
    )
    p3 = resp3.json()["data"]["pagination"]
    assert p3["total_pages"] == 3 and p3["has_more"] is False


def test_sessions_out_of_range_page_returns_empty():
    """越界页 → 200 + 空列表 + has_more=False（不报错）。"""
    service = _make_service(total=3)  # 3 条 / 15 → 1 页
    client = TestClient(_make_app(service))

    resp = client.get(
        "/sessions", params={"page": 99}, headers=_auth_headers(),
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["sessions"] == []
    assert data["pagination"]["has_more"] is False
    assert data["pagination"]["total_pages"] == 1


def test_sessions_top_sessions_always_present():
    """置顶会话与当前页会话都返回，且结构含 SessionMeta 字段。"""
    top = [_meta("p1"), _meta("p2")]
    sessions = [_meta("s1")]
    service = _make_service(top=top, sessions=sessions, total=20)
    client = TestClient(_make_app(service))

    resp = client.get(
        "/sessions", params={"page": 2, "page_size": 15}, headers=_auth_headers(),
    )

    data = resp.json()["data"]
    assert [s["session_id"] for s in data["top_sessions"]] == ["p1", "p2"]
    assert [s["session_id"] for s in data["sessions"]] == ["s1"]
    for item in data["top_sessions"] + data["sessions"]:
        assert set(item.keys()) == {
            "session_id", "user_id", "name", "created_at",
            "updated_at", "message_count", "agent_ids",
        }


def test_sessions_invalid_params_rejected():
    """page=0 / page_size=0 / page_size=101 → 422（FastAPI Query 校验）。"""
    client = TestClient(_make_app())
    for params in (
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
        {"page": -1},
    ):
        resp = client.get("/sessions", params=params, headers=_auth_headers())
        assert resp.status_code == 422, params


def test_sessions_401_without_jwt():
    client = TestClient(_make_app())
    resp = client.get("/sessions")
    assert resp.status_code == 401
