"""dashboard 越权修复测试：认证 + 制度问答权限 + kb_id 归属校验。"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.dashboard as dashboard_route
import tools.policy_qa_tools as tool_module
from app.routes.dashboard import router
from app.services.auth_service import create_access_token


@pytest.fixture(autouse=True)
def _isolate_kb_map(monkeypatch):
    """屏蔽环境变量映射，回退默认假表 "123"/"456"。"""
    monkeypatch.setattr(tool_module, "POLICY_QA_KB_MAP", "")


def _auth_headers() -> dict:
    token = create_access_token({"user_id": "u1", "department": "研发部"})
    return {"Authorization": f"Bearer {token}"}


def _perms_data(names):
    return {
        "permissions": {
            "agent_whitelist": [{"name": n} for n in names],
            "skill_blacklist": [],
        }
    }


def _make_app():
    app = FastAPI()
    app.include_router(router)
    app.state.redis_client = AsyncMock()
    app.state.regulations_gap_dao = MagicMock()
    app.state.regulations_gap_service = MagicMock()
    return app


def test_kb_overview_requires_auth():
    """无 JWT → 401（修复前为 200）。"""
    client = TestClient(_make_app())
    resp = client.get("/dashboard/kb-overview")
    assert resp.status_code == 401


def test_kb_overview_rejects_no_permission(monkeypatch):
    """登录但无制度问答权限 → 403；登录态过期 → 401。"""
    client = TestClient(_make_app())

    monkeypatch.setattr(
        dashboard_route, "get_user_permissions", AsyncMock(return_value=None),
    )
    resp = client.get("/dashboard/kb-overview", headers=_auth_headers())
    assert resp.status_code == 401

    monkeypatch.setattr(
        dashboard_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["别的智能体"])),
    )
    resp = client.get("/dashboard/kb-overview", headers=_auth_headers())
    assert resp.status_code == 403


def test_resolve_rejects_foreign_kb_id(monkeypatch):
    """kb_id 不在权限范围 → 403，不执行写操作（修复前未登录即可写）。"""
    monkeypatch.setattr(
        dashboard_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答"])),
    )
    app = _make_app()
    app.state.regulations_gap_service.resolve_gaps = AsyncMock()
    client = TestClient(app)

    resp = client.post(
        "/dashboard/knowledge-gaps/resolve",
        json={"kb_id": "kb_sneak_999"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 403
    app.state.regulations_gap_service.resolve_gaps.assert_not_awaited()


def test_resolve_allows_permitted_kb_id(monkeypatch):
    """kb_id 在权限内 → 正常闭合并返回 resolved_count。"""
    monkeypatch.setattr(
        dashboard_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答"])),
    )
    monkeypatch.setattr(
        dashboard_route, "invalidate_dashboard_cache", MagicMock(),
    )
    app = _make_app()
    app.state.regulations_gap_service.resolve_gaps = AsyncMock(return_value=3)
    client = TestClient(app)

    resp = client.post(
        "/dashboard/knowledge-gaps/resolve",
        json={"kb_id": "123"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["resolved_count"] == 3
    app.state.regulations_gap_service.resolve_gaps.assert_awaited_once_with(
        gap_ids=None, kb_id="123", allowed_kb_ids=["123"],
    )


def test_resolve_passes_allowed_scope_for_gap_ids(monkeypatch):
    """gap_ids 路径也必须携带授权库范围（防'合法 kb_id + 越权 gap_ids'组合绕过）。"""
    monkeypatch.setattr(
        dashboard_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答"])),
    )
    monkeypatch.setattr(
        dashboard_route, "invalidate_dashboard_cache", MagicMock(),
    )
    app = _make_app()
    app.state.regulations_gap_service.resolve_gaps = AsyncMock(return_value=1)
    client = TestClient(app)

    resp = client.post(
        "/dashboard/knowledge-gaps/resolve",
        json={"gap_ids": ["g1", "g2"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    app.state.regulations_gap_service.resolve_gaps.assert_awaited_once_with(
        gap_ids=["g1", "g2"], kb_id=None, allowed_kb_ids=["123"],
    )
