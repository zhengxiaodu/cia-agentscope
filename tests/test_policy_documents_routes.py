"""文档接口（/api/v1/policy/documents/*）路由单测。

覆盖：
- 列文档：kb_ids 后端权限解析（前端不传）、knowledge_bases 联动过滤、
  无权限返回空列表不调 kb_app、kb_app 异常 502、登录态过期 401
- 源文档：严格 doc_id 归属校验（权限内命中才转发）、404 统一文案、
  200 时 Content-Type / Content-Disposition / X-Page-Count 透传
"""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.policy_qa as policy_qa_route
import tools.policy_qa_tools as tool_module
from app.routes.policy_qa import router
from app.regulations.services.kb_client import KBClientError
from app.services.auth_service import create_access_token


@pytest.fixture(autouse=True)
def _isolate_kb_map(monkeypatch):
    """屏蔽环境变量 POLICY_QA_KB_MAP（内网配置真实 kb_id 的 JSON），
    使映射回退到代码内默认假表（"123"/"456"），测试对任意环境免疫。"""
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


def _make_app(kb_client):
    app = FastAPI()
    app.include_router(router)
    app.state.redis_client = AsyncMock()
    app.state.regulations_kb_client = kb_client
    return app


def _kb_client(query_return=None, query_side_effect=None, file_resp=None):
    kb = MagicMock()
    kb.query_documents = AsyncMock(
        return_value=query_return, side_effect=query_side_effect,
    )
    kb.get_document_file = AsyncMock(return_value=file_resp)
    return kb


# ---------------------------------------------------------------------------
# 列文档 POST /api/v1/policy/documents/query
# ---------------------------------------------------------------------------

def test_query_documents_full_scope(monkeypatch):
    """全选（不传 knowledge_bases）→ 权限解析的 kb_ids 全量传给 kb_app。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答", "信科制度问答"])),
    )
    data = {"items": [{"doc_id": "d1"}], "total": 1, "page": 1, "page_size": 20}
    kb = _kb_client(query_return=data)
    client = TestClient(_make_app(kb))

    resp = client.post(
        "/api/v1/policy/documents/query",
        json={"page": 1, "page_size": 20},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["data"] == data
    kb.query_documents.assert_awaited_once_with(
        ["123", "456"], page=1, page_size=20, search="",
    )


def test_query_documents_kb_selection_filters(monkeypatch):
    """knowledge_bases 联动过滤 → 只传所选库的 kb_ids。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答", "信科制度问答"])),
    )
    kb = _kb_client(query_return={"items": [], "total": 0, "page": 1, "page_size": 20})
    client = TestClient(_make_app(kb))

    resp = client.post(
        "/api/v1/policy/documents/query",
        json={"knowledge_bases": ["信科制度问答"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert kb.query_documents.await_args.args[0] == ["456"]


def test_query_documents_no_permission_returns_empty(monkeypatch):
    """无制度问答权限（或选择交集为空）→ 空列表，不调 kb_app。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["别的智能体"])),
    )
    kb = _kb_client()
    client = TestClient(_make_app(kb))

    resp = client.post(
        "/api/v1/policy/documents/query",
        json={},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["items"] == []
    assert data["total"] == 0
    kb.query_documents.assert_not_called()


def test_query_documents_kb_error_maps_502(monkeypatch):
    """kb_app 业务/网络失败 → 502。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答"])),
    )
    kb = _kb_client(query_side_effect=KBClientError("kb down"))
    client = TestClient(_make_app(kb))

    resp = client.post(
        "/api/v1/policy/documents/query", json={}, headers=_auth_headers(),
    )
    assert resp.status_code == 502


def test_query_documents_session_expired_401(monkeypatch):
    """Redis 无权限缓存（登录态过期）→ 401。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions", AsyncMock(return_value=None),
    )
    client = TestClient(_make_app(_kb_client()))

    resp = client.post(
        "/api/v1/policy/documents/query", json={}, headers=_auth_headers(),
    )
    assert resp.status_code == 401


def test_query_documents_requires_auth():
    """无 JWT → 401（current_user 依赖拦截）。"""
    client = TestClient(_make_app(_kb_client()))
    resp = client.post("/api/v1/policy/documents/query", json={})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 源文档 GET /api/v1/policy/documents/{doc_id}/file
# ---------------------------------------------------------------------------

def _file_response(status=200):
    return httpx.Response(
        status,
        headers={
            "content-type": "application/pdf",
            "content-disposition": 'inline; filename="doc.pdf"',
            "x-page-count": "12",
        },
        content=b"%PDF-fake-bytes",
    )


def test_document_file_in_scope_passes_through(monkeypatch):
    """doc_id 在权限内文档中 → 转发文件流并透传三个头。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答"])),
    )
    pages = [[{"doc_id": "d1", "filename": "差旅报销办法"}]]
    kb = MagicMock()
    kb.query_documents = AsyncMock(
        side_effect=lambda kb_ids, page=1, page_size=100, search="": {
            "items": pages[0] if page == 1 else [],
            "total": 1,
            "page": page,
            "page_size": page_size,
        }
    )
    kb.get_document_file = AsyncMock(return_value=_file_response())
    client = TestClient(_make_app(kb))

    resp = client.get(
        "/api/v1/policy/documents/d1/file", headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.content == b"%PDF-fake-bytes"
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["x-page-count"] == "12"
    assert "doc.pdf" in resp.headers["content-disposition"]
    # 校验遍历用全量 kb_ids（不受前端选择过滤），每页 100
    assert kb.query_documents.await_args.kwargs["page_size"] == 100


def test_document_file_out_of_scope_404(monkeypatch):
    """doc_id 不在权限内文档中 → 404 统一文案，不调文件接口。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答"])),
    )
    kb = MagicMock()
    kb.query_documents = AsyncMock(return_value={
        "items": [{"doc_id": "别的文档"}], "total": 1, "page": 1, "page_size": 100,
    })
    kb.get_document_file = AsyncMock()
    client = TestClient(_make_app(kb))

    resp = client.get(
        "/api/v1/policy/documents/d-not-exist/file", headers=_auth_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "文档不存在或您无权查看"
    kb.get_document_file.assert_not_called()


def test_document_file_no_permission_404(monkeypatch):
    """无任何知识库权限 → kb_ids 为空直接 404，不调 kb_app。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["别的智能体"])),
    )
    kb = _kb_client()
    client = TestClient(_make_app(kb))

    resp = client.get(
        "/api/v1/policy/documents/d1/file", headers=_auth_headers(),
    )
    assert resp.status_code == 404
    kb.query_documents.assert_not_called()


def test_document_file_kb_upstream_404_maps_404(monkeypatch):
    """归属校验通过但 kb_app 返回非 200（源文件缺失等）→ 转 404。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答"])),
    )
    kb = MagicMock()
    kb.query_documents = AsyncMock(return_value={
        "items": [{"doc_id": "d1"}], "total": 1, "page": 1, "page_size": 100,
    })
    kb.get_document_file = AsyncMock(return_value=_file_response(status=404))
    client = TestClient(_make_app(kb))

    resp = client.get(
        "/api/v1/policy/documents/d1/file", headers=_auth_headers(),
    )
    assert resp.status_code == 404


def test_document_file_requires_auth():
    """无 JWT → 401。"""
    client = TestClient(_make_app(_kb_client()))
    resp = client.get("/api/v1/policy/documents/d1/file")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 越权修复：POST /api/v1/policy/qa 的 knowledge_base_ids 权限交集
# ---------------------------------------------------------------------------

def _qa_app(svc):
    app = _make_app(MagicMock())
    app.state.policy_qa_service = svc
    return app


def _qa_service():
    svc = MagicMock()
    svc.run_general_qa = AsyncMock(return_value={"answer": "ok"})
    return svc


def test_policy_qa_allows_permitted_kb_ids(monkeypatch):
    """请求 kb_ids 全部在权限内 → 透传给服务。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答", "信科制度问答"])),
    )
    svc = _qa_service()
    client = TestClient(_qa_app(svc))

    resp = client.post(
        "/api/v1/policy/qa",
        json={"question": "q", "knowledge_base_ids": ["123", "456"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert svc.run_general_qa.await_args.kwargs["kb_ids"] == ["123", "456"]


def test_policy_qa_silently_drops_unpermitted_kb_ids(monkeypatch):
    """混合请求：越权 kb_id 被剔除，仅传权限内部分。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答"])),
    )
    svc = _qa_service()
    client = TestClient(_qa_app(svc))

    resp = client.post(
        "/api/v1/policy/qa",
        json={"question": "q", "knowledge_base_ids": ["123", "kb_sneak_999"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert svc.run_general_qa.await_args.kwargs["kb_ids"] == ["123"]


def test_policy_qa_rejects_all_foreign_kb_ids(monkeypatch):
    """请求 kb_ids 全部越权 → 403，不调服务。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=_perms_data(["金科制度问答"])),
    )
    svc = _qa_service()
    client = TestClient(_qa_app(svc))

    resp = client.post(
        "/api/v1/policy/qa",
        json={"question": "q", "knowledge_base_ids": ["kb_a", "kb_b"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 403
    svc.run_general_qa.assert_not_awaited()


def test_policy_qa_rejects_expired_login(monkeypatch):
    """Redis 无权限缓存（登录过期）→ 401。"""
    monkeypatch.setattr(
        policy_qa_route, "get_user_permissions",
        AsyncMock(return_value=None),
    )
    svc = _qa_service()
    client = TestClient(_qa_app(svc))

    resp = client.post(
        "/api/v1/policy/qa",
        json={"question": "q", "knowledge_base_ids": ["123"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 401
    svc.run_general_qa.assert_not_awaited()
