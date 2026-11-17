"""制度问答与数据大盘路由单测：401 鉴权 / blocking 响应结构 / 错误映射 / 大盘接口。

无现成 FastAPI TestClient 基建，本文件自建最小 app（仅挂被测路由 + 真 JWT 签发），
不依赖主应用 lifespan（MySQL/Redis 等服务全部以 app.state mock 注入）。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tools.policy_qa_tools as tool_module
from app.routes import dashboard as dashboard_route
from app.routes import policy_qa as policy_qa_route
from app.regulations.schemas import GeneralQAResponse, RetrieverResource
from app.regulations.services.kb_client import KBClientError
from app.services.auth_service import create_access_token


@pytest.fixture(autouse=True)
def _isolate_kb_map(monkeypatch):
    """屏蔽环境变量 POLICY_QA_KB_MAP，回退代码内默认假表（"123"/"456"）。"""
    monkeypatch.setattr(tool_module, "POLICY_QA_KB_MAP", "")


def _grant_perms(monkeypatch, names=("金科制度问答", "信科制度问答")):
    """mock 登录态：授予制度问答权限（映射到假表 kb_id "123"/"456"）。"""
    for mod in (policy_qa_route, dashboard_route):
        monkeypatch.setattr(
            mod, "get_user_permissions",
            AsyncMock(return_value={
                "permissions": {
                    "agent_whitelist": [{"name": n} for n in names],
                    "skill_blacklist": [],
                },
            }),
        )


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(policy_qa_route.router)
    app.include_router(dashboard_route.router)
    app.state.redis_client = AsyncMock()  # 越权修复后权限校验依赖 Redis
    return app


def _auth_headers(payload=None) -> dict:
    token = create_access_token(payload or {"user_id": "u1", "department": "研发部"})
    return {"Authorization": f"Bearer {token}"}


def _qa_response():
    return GeneralQAResponse(
        answer="报销标准如下",
        citations=[RetrieverResource(
            position=1, dataset_id="kb1", dataset_name="制度库",
            document_id="d1", document_name="差旅报销办法", segment_id="s1",
            score=0.9, page_start=1, page_end=2,
        )],
        app_serial_number="serial-1", model="test-model", created_at=1750000000,
    )


def _mock_service(response=None, error=None):
    svc = MagicMock()
    if error is not None:
        svc.run_general_qa = AsyncMock(side_effect=error)
    else:
        svc.run_general_qa = AsyncMock(return_value=response or _qa_response())
    return svc


# ---- POST /api/v1/policy/qa ----

def test_policy_qa_401_without_jwt():
    app = _make_app()
    app.state.policy_qa_service = _mock_service()
    client = TestClient(app)

    resp = client.post("/api/v1/policy/qa", json={
        "question": "q", "knowledge_base_ids": ["kb1"],
    })
    assert resp.status_code == 401


def test_policy_qa_401_invalid_jwt():
    app = _make_app()
    app.state.policy_qa_service = _mock_service()
    client = TestClient(app)

    resp = client.post(
        "/api/v1/policy/qa",
        json={"question": "q", "knowledge_base_ids": ["kb1"]},
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert resp.status_code == 401


def test_policy_qa_blocking_response_structure(monkeypatch):
    """blocking：响应顶层直接是 answer/citations/app_serial_number/model/created_at。"""
    _grant_perms(monkeypatch)
    app = _make_app()
    service = _mock_service()
    app.state.policy_qa_service = service
    client = TestClient(app)

    resp = client.post(
        "/api/v1/policy/qa",
        json={
            "question": "差旅报销标准？",
            "knowledge_base_ids": ["123", "456"],
            "app_serial_number": "serial-1",
        },
        headers=_auth_headers(),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {
        "answer", "citations", "app_serial_number", "model", "created_at",
    }
    assert data["answer"] == "报销标准如下"
    assert data["app_serial_number"] == "serial-1"
    assert data["model"] == "test-model"
    assert data["citations"][0]["document_name"] == "差旅报销办法"

    # JWT 中的 user_id/department 透传给服务；kb_ids 为权限交集后的值
    service.run_general_qa.assert_awaited_once()
    kwargs = service.run_general_qa.await_args.kwargs
    assert kwargs["question"] == "差旅报销标准？"
    assert kwargs["kb_ids"] == ["123", "456"]
    assert kwargs["serial"] == "serial-1"
    assert kwargs["user_id"] == "u1"
    assert kwargs["user_department"] == "研发部"


def test_policy_qa_kb_error_maps_502(monkeypatch):
    """KBClientError → 502。"""
    _grant_perms(monkeypatch)
    app = _make_app()
    app.state.policy_qa_service = _mock_service(error=KBClientError("检索失败"))
    client = TestClient(app)

    resp = client.post(
        "/api/v1/policy/qa",
        json={"question": "q", "knowledge_base_ids": ["123"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 502
    assert "知识库检索失败" in resp.json()["detail"]


def test_policy_qa_internal_error_maps_500(monkeypatch):
    _grant_perms(monkeypatch)
    app = _make_app()
    app.state.policy_qa_service = _mock_service(error=RuntimeError("boom"))
    client = TestClient(app)

    resp = client.post(
        "/api/v1/policy/qa",
        json={"question": "q", "knowledge_base_ids": ["123"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 500


def test_policy_qa_service_not_ready_503():
    """app.state 未注入服务 → 503。"""
    app = _make_app()
    client = TestClient(app)

    resp = client.post(
        "/api/v1/policy/qa",
        json={"question": "q", "knowledge_base_ids": ["kb1"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 503


def test_policy_qa_validation_422():
    """空 question / 空 knowledge_base_ids → 422。"""
    app = _make_app()
    app.state.policy_qa_service = _mock_service()
    client = TestClient(app)

    resp = client.post(
        "/api/v1/policy/qa",
        json={"question": "", "knowledge_base_ids": []},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


# ---- GET /dashboard/kb-overview ----

def test_dashboard_kb_overview_structure(monkeypatch):
    """Mock 模式：统一响应壳 code/message/data，data 六块齐全。"""
    _grant_perms(monkeypatch)
    # 内网真实数据模式下 build_dashboard_data 会访问 DAO，
    # 测试强制 Mock 分支（同事 test_regulations_dashboard.py 同款模式）
    from app.regulations.services import dashboard_service as dsvc
    monkeypatch.setattr(dsvc, "REGULATIONS_DASHBOARD_USE_REAL_DATA", False)
    app = _make_app()
    app.state.regulations_gap_dao = MagicMock()
    client = TestClient(app)

    resp = client.get("/dashboard/kb-overview", headers=_auth_headers())
    assert resp.status_code == 200

    body = resp.json()
    assert body["code"] == 200
    assert body["message"] == "success"
    assert set(body["data"].keys()) == {
        "overview", "search_trend", "department_distribution",
        "department_usage_ranking", "top_cited_knowledge", "knowledge_gaps",
    }
    assert set(body["data"]["overview"].keys()) == {
        "published_knowledge", "active_search_users", "valid_search_count",
        "empty_answer_rate", "search_p95_latency", "active_kb_count", "total_kb_count",
    }


def test_dashboard_kb_overview_401_without_jwt():
    app = _make_app()
    app.state.regulations_gap_dao = MagicMock()
    client = TestClient(app)

    resp = client.get("/dashboard/kb-overview")
    assert resp.status_code == 401


def test_dashboard_kb_overview_503_when_dao_missing(monkeypatch):
    _grant_perms(monkeypatch)
    app = _make_app()
    client = TestClient(app)

    resp = client.get("/dashboard/kb-overview", headers=_auth_headers())
    assert resp.status_code == 503


# ---- POST /dashboard/knowledge-gaps/resolve ----

def test_dashboard_resolve_requires_ids_400(monkeypatch):
    """gap_ids 与 kb_id 均未提供 → 400。"""
    _grant_perms(monkeypatch)
    app = _make_app()
    app.state.regulations_gap_service = MagicMock()
    client = TestClient(app)

    resp = client.post(
        "/dashboard/knowledge-gaps/resolve", json={}, headers=_auth_headers(),
    )
    assert resp.status_code == 400


def test_dashboard_resolve_success(monkeypatch):
    """按 kb_id 批量闭合 → resolved_count，并携带授权范围调用服务。"""
    _grant_perms(monkeypatch)
    app = _make_app()
    gap_service = MagicMock()
    gap_service.resolve_gaps = AsyncMock(return_value=3)
    app.state.regulations_gap_service = gap_service
    client = TestClient(app)

    resp = client.post(
        "/dashboard/knowledge-gaps/resolve",
        json={"kb_id": "123"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["data"]["resolved_count"] == 3
    gap_service.resolve_gaps.assert_awaited_once_with(
        gap_ids=None, kb_id="123", allowed_kb_ids=["123", "456"],
    )


def test_dashboard_resolve_401_without_jwt():
    app = _make_app()
    client = TestClient(app)

    resp = client.post(
        "/dashboard/knowledge-gaps/resolve", json={"kb_id": "kb1"},
    )
    assert resp.status_code == 401
