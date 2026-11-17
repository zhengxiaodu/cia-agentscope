"""GET /health/llm 大模型服务探测接口测试。

探测接口行为：
- 成功 → 200 + status ok + 模型回复
- 上游返回错误状态码 → 透传状态码与报错信息
- 连接失败 / 超时 → 502 / 504
- 模型配置缺失 → 500
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError, APITimeoutError
from unittest.mock import AsyncMock, MagicMock

import app.routes.health as health_route
from app.routes.health import router

DEFAULT_CFG = {
    "model_name": "test-model",
    "base_url": "http://llm.local/v1",
    "api_key": "sk-test",
}


def _make_app(with_config: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if with_config:
        app.state.model_config = {"models": {"default": dict(DEFAULT_CFG)}}
    return app


def _mock_client(raise_exc=None, reply: str = "pong"):
    """构造 mock 的 AsyncOpenAI 客户端，chat.completions.create 可抛异常或返回固定回复。"""
    client = MagicMock()
    create = AsyncMock()
    if raise_exc is not None:
        create.side_effect = raise_exc
    else:
        message = MagicMock()
        message.content = reply
        choice = MagicMock()
        choice.message = message
        response = MagicMock()
        response.choices = [choice]
        create.return_value = response
    client.chat.completions.create = create
    return client


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(
        health_route, "create_async_client",
        lambda cfg, timeout=None: client,
    )


def test_llm_health_ok(monkeypatch):
    """服务可用 → 200，返回模型名与回复。"""
    _patch_client(monkeypatch, _mock_client(reply="pong"))
    resp = TestClient(_make_app()).get("/health/llm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model"] == "test-model"
    assert data["base_url"] == "http://llm.local/v1"
    assert data["reply"] == "pong"
    assert data["latency_ms"] >= 0


def test_llm_health_fake_key_401_alive(monkeypatch):
    """虚构 key 被网关拒绝（401）→ 网关存活：200 + status ok。"""
    captured = {}

    def _factory(cfg, timeout=None):
        captured["cfg"] = cfg
        return _mock_client(raise_exc=APIStatusError(
            "Error code: 401 - invalid api key",
            response=httpx.Response(
                401, request=httpx.Request("POST", "http://llm.local/v1")),
            body=None,
        ))

    monkeypatch.setattr(health_route, "create_async_client", _factory)
    resp = TestClient(_make_app()).get("/health/llm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    # 探测请求使用虚构 key，而非配置中的真实 key
    assert captured["cfg"]["api_key"] != DEFAULT_CFG["api_key"]


def test_llm_health_upstream_5xx_passthrough(monkeypatch):
    """上游 503 → HTTP 503 + 上游报错信息。"""
    response = httpx.Response(503, request=httpx.Request("POST", "http://llm.local/v1"))
    exc = APIStatusError("Error code: 503 - model overloaded",
                         response=response, body=None)
    _patch_client(monkeypatch, _mock_client(raise_exc=exc))
    resp = TestClient(_make_app()).get("/health/llm")
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["status_code"] == 503
    assert "503" in data["error"]["message"]


def test_llm_health_timeout(monkeypatch):
    """请求超时 → 504。"""
    exc = APITimeoutError(request=httpx.Request("POST", "http://llm.local/v1"))
    _patch_client(monkeypatch, _mock_client(raise_exc=exc))
    resp = TestClient(_make_app()).get("/health/llm")
    assert resp.status_code == 504
    data = resp.json()
    assert data["status"] == "error"
    assert data["error"]["type"] == "timeout"


def test_llm_health_connection_error(monkeypatch):
    """连接失败（无上游状态码）→ 502。"""
    exc = APIConnectionError(message="Connection error.",
                             request=httpx.Request("POST", "http://llm.local/v1"))
    _patch_client(monkeypatch, _mock_client(raise_exc=exc))
    resp = TestClient(_make_app()).get("/health/llm")
    assert resp.status_code == 502
    data = resp.json()
    assert data["status"] == "error"
    assert data["error"]["type"] == "connection_error"


def test_llm_health_missing_config():
    """模型配置未加载 → 500。"""
    resp = TestClient(_make_app(with_config=False)).get("/health/llm")
    assert resp.status_code == 500
    data = resp.json()
    assert data["status"] == "error"
    assert data["error"]["type"] == "config_error"
