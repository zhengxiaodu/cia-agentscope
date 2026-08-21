"""安全敏感内容检测单测：check_sensitive 命中/放行/兜底 + Langfuse 埋点 + chat 入口拦截。"""
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx as real_httpx
import pytest

import app.services.sensitive_service as sensitive_service
from app.services.sensitive_service import (
    build_message_replace_event,
    check_sensitive,
)


# ---- mock 基础设施 ----

class _MockResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _MockAsyncClient:
    """替代 httpx.AsyncClient：可注入响应或异常，记录 post 调用。"""

    instances = []

    def __init__(self, response=None, exc=None, timeout=None):
        self._response = response
        self._exc = exc
        self.post_calls = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        self.post_calls.append({"url": url, "json": json})
        if self._exc:
            raise self._exc
        return self._response


def _install_httpx(monkeypatch, response=None, exc=None):
    """将 sensitive_service 模块内的 httpx 替换为 mock（保留真实异常类）。"""
    _MockAsyncClient.instances = []
    fake_httpx = SimpleNamespace(
        AsyncClient=lambda **kwargs: _MockAsyncClient(response=response, exc=exc),
        TimeoutException=real_httpx.TimeoutException,
    )
    monkeypatch.setattr(sensitive_service, "httpx", fake_httpx)
    monkeypatch.setattr(
        sensitive_service, "SENSITIVE_SERVICE_URL", "http://fake/sensitive/check"
    )


def _hit_response():
    return _MockResponse({
        "code": 0,
        "hasSensitiveWord": True,
        "sensitiveWords": ["枪杀"],
        "semantic": {
            "label": "unsafe", "score": 0.99, "category": "violence",
            "reason": "暴力伤害意图", "source": "model",
        },
    })


def _safe_response():
    return _MockResponse({
        "code": 0,
        "hasSensitiveWord": False,
        "sensitiveWords": [],
        "semantic": {
            "label": "safe", "score": 0.01, "category": "0",
            "reason": "正常文本", "source": "model",
        },
    })


# ---- check_sensitive 核心逻辑 ----

@pytest.mark.asyncio
async def test_check_sensitive_hit(monkeypatch):
    """命中：code=0 且 hasSensitiveWord=true → blocked=True 及原因/分类。"""
    _install_httpx(monkeypatch, response=_hit_response())
    result = await check_sensitive("我要枪杀某人", stage="input")
    assert result["blocked"] is True
    assert result["reason"] == "暴力伤害意图"
    assert result["category"] == "violence"
    assert result["sensitive_words"] == ["枪杀"]
    assert result["raw"]["code"] == 0
    # 请求体包含文本与阈值
    client = _MockAsyncClient.instances[-1]
    assert client.post_calls[0]["json"]["text"] == "我要枪杀某人"
    assert client.post_calls[0]["json"]["threshold"] == sensitive_service.SENSITIVE_THRESHOLD


@pytest.mark.asyncio
async def test_check_sensitive_safe(monkeypatch):
    """未命中：hasSensitiveWord=false → 放行。"""
    _install_httpx(monkeypatch, response=_safe_response())
    result = await check_sensitive("今天天气很好", stage="input")
    assert result["blocked"] is False
    assert result["raw"]["hasSensitiveWord"] is False


@pytest.mark.asyncio
async def test_check_sensitive_url_empty(monkeypatch):
    """兜底：服务地址未配置 → 跳过检测，不发请求。"""
    _install_httpx(monkeypatch, response=_hit_response())
    monkeypatch.setattr(sensitive_service, "SENSITIVE_SERVICE_URL", "")
    result = await check_sensitive("任意文本", stage="input")
    assert result["blocked"] is False
    assert result["source"] == "client-skip"
    assert _MockAsyncClient.instances == []


@pytest.mark.asyncio
async def test_check_sensitive_empty_text(monkeypatch):
    """兜底：空文本直接放行（服务约定空文本未命中），不发请求。"""
    _install_httpx(monkeypatch, response=_hit_response())
    result = await check_sensitive("   ", stage="input")
    assert result["blocked"] is False
    assert _MockAsyncClient.instances == []


@pytest.mark.asyncio
async def test_check_sensitive_timeout(monkeypatch):
    """兜底：超时 → 放行。"""
    _install_httpx(
        monkeypatch,
        exc=real_httpx.TimeoutException("timed out"),
    )
    result = await check_sensitive("文本", stage="output")
    assert result["blocked"] is False
    assert result["source"] == "client-fallback"


@pytest.mark.asyncio
async def test_check_sensitive_network_error(monkeypatch):
    """兜底：连接失败/HTTP 非 2xx → 放行。"""
    _install_httpx(monkeypatch, exc=real_httpx.ConnectError("refused"))
    result = await check_sensitive("文本", stage="input")
    assert result["blocked"] is False
    assert result["source"] == "client-fallback"


@pytest.mark.asyncio
async def test_check_sensitive_business_error_code(monkeypatch):
    """兜底：code!=0（业务异常，如 50001 模型异常）→ 放行。"""
    _install_httpx(monkeypatch, response=_MockResponse({
        "code": 50001, "hasSensitiveWord": True, "sensitiveWords": [],
        "semantic": {"label": "review", "category": "system_error", "reason": "模型异常"},
    }))
    result = await check_sensitive("文本", stage="input")
    assert result["blocked"] is False
    assert result["source"] == "client-fallback"


@pytest.mark.asyncio
async def test_check_sensitive_missing_field(monkeypatch):
    """兜底：响应缺少 hasSensitiveWord 字段 → 放行。"""
    _install_httpx(monkeypatch, response=_MockResponse({"code": 0}))
    result = await check_sensitive("文本", stage="input")
    assert result["blocked"] is False
    assert result["source"] == "client-fallback"


# ---- Langfuse 埋点 ----

@pytest.mark.asyncio
async def test_check_sensitive_langfuse_span(monkeypatch):
    """每次调用创建 sensitive-check span，记录输入/输出/耗时/stage。"""
    _install_httpx(monkeypatch, response=_hit_response())

    span_updates = []

    @contextmanager
    def fake_start_span(name, input=None):
        span = MagicMock()
        span.name = name
        span.input = input
        span.update = lambda **kw: span_updates.append({"name": name, "input": input, **kw})
        yield span

    mock_svc = MagicMock()
    mock_svc.enabled = True
    mock_svc.start_span = fake_start_span
    monkeypatch.setattr(sensitive_service, "get_current_langfuse", lambda: mock_svc)

    result = await check_sensitive("我要枪杀某人", stage="output")

    assert result["blocked"] is True
    assert len(span_updates) == 1
    upd = span_updates[0]
    assert upd["name"] == "sensitive-check"
    assert upd["input"]["text"] == "我要枪杀某人"
    assert upd["input"]["stage"] == "output"
    assert upd["output"]["blocked"] is True
    assert upd["output"]["reason"] == "暴力伤害意图"
    assert upd["output"]["category"] == "violence"
    assert upd["metadata"]["stage"] == "output"
    assert "latency_ms" in upd["metadata"]


@pytest.mark.asyncio
async def test_check_sensitive_langfuse_disabled(monkeypatch):
    """langfuse 未启用时不创建 span，检测仍正常。"""
    _install_httpx(monkeypatch, response=_hit_response())
    monkeypatch.setattr(sensitive_service, "get_current_langfuse", lambda: None)
    result = await check_sensitive("文本", stage="input")
    assert result["blocked"] is True


# ---- message_replace 事件构造 ----

def test_build_message_replace_event_with_reason():
    event = build_message_replace_event(
        {"reason": "暴力伤害意图", "category": "violence"}, stage="input"
    )
    assert event["type"] == "message_replace"
    assert event["stage"] == "input"
    assert event["reason"] == "暴力伤害意图"
    assert "暴力伤害意图" in event["message"]
    assert "请修改提问" in event["message"]


def test_build_message_replace_event_without_reason():
    event = build_message_replace_event({"reason": ""}, stage="output")
    assert event["type"] == "message_replace"
    assert event["stage"] == "output"
    assert event["reason"] == ""
    assert event["message"] == "您的内容涉及敏感词，请修改提问"


# ---- /chat 入口：用户输入检测 ----

def _build_request():
    request = MagicMock()
    request.headers = {}
    session_service = MagicMock()
    session_service.get_or_create_session = AsyncMock(return_value="sess-1")
    request.app.state.session_service = session_service
    request.app.state.chat_tasks = {}
    return request


@pytest.mark.asyncio
async def test_chat_input_blocked_emits_message_replace(monkeypatch):
    """用户输入命中：发送 message_replace 事件，不进入 generate_response。"""
    from app.models.chat import ChatRequest
    from app.routes import chat as chat_route

    async def fake_check(text, stage="input"):
        return {
            "blocked": True, "reason": "暴力伤害意图", "category": "violence",
            "sensitive_words": [], "source": "model", "raw": {},
        }

    monkeypatch.setattr(chat_route, "check_sensitive", fake_check)

    gen_calls = {"n": 0}

    async def fake_generate_response(**kwargs):
        gen_calls["n"] += 1
        yield "data: {\"type\": \"summary\", \"content\": \"x\"}\n\n"

    monkeypatch.setattr(chat_route, "generate_response", fake_generate_response)

    request = _build_request()
    body = ChatRequest(
        messages=[{"role": "user", "content": "我要枪杀某人"}],
        session_id="sess-1",
    )

    resp = await chat_route.chat(request, body, {"user_id": "u1"})
    events = []
    async for chunk in resp.body_iterator:
        events.append(chunk)

    # 不进入主流程
    assert gen_calls["n"] == 0
    # 事件序列：session_ready → message_replace
    parsed = [json.loads(e[6:].strip()) for e in events]
    assert [p["type"] for p in parsed] == ["session_ready", "message_replace"]
    replace_ev = parsed[1]
    assert replace_ev["stage"] == "input"
    assert replace_ev["reason"] == "暴力伤害意图"
    assert "敏感" in replace_ev["message"]
    # 流结束后注册表清理
    assert "sess-1" not in request.app.state.chat_tasks


@pytest.mark.asyncio
async def test_chat_input_safe_calls_generate_response(monkeypatch):
    """用户输入未命中：正常进入 generate_response。"""
    from app.models.chat import ChatRequest
    from app.routes import chat as chat_route

    async def fake_check(text, stage="input"):
        return {
            "blocked": False, "reason": "", "category": "",
            "sensitive_words": [], "source": "model", "raw": {},
        }

    monkeypatch.setattr(chat_route, "check_sensitive", fake_check)

    gen_calls = {"n": 0}

    async def fake_generate_response(**kwargs):
        gen_calls["n"] += 1
        yield "data: {\"type\": \"summary\", \"content\": \"hello\"}\n\n"

    monkeypatch.setattr(chat_route, "generate_response", fake_generate_response)

    request = _build_request()
    body = ChatRequest(
        messages=[{"role": "user", "content": "今天天气很好"}],
        session_id="sess-1",
    )

    resp = await chat_route.chat(request, body, {"user_id": "u1"})
    events = []
    async for chunk in resp.body_iterator:
        events.append(chunk)

    assert gen_calls["n"] == 1
    parsed = [json.loads(e[6:].strip()) for e in events]
    assert "message_replace" not in [p["type"] for p in parsed]
    assert parsed[-1]["type"] == "summary"


@pytest.mark.asyncio
async def test_chat_input_fallback_continues(monkeypatch):
    """兜底：检测服务异常时放行，主流程不受影响。"""
    from app.models.chat import ChatRequest
    from app.routes import chat as chat_route

    async def fake_check(text, stage="input"):
        return {
            "blocked": False, "reason": "timeout", "category": "",
            "sensitive_words": [], "source": "client-fallback", "raw": None,
        }

    monkeypatch.setattr(chat_route, "check_sensitive", fake_check)

    async def fake_generate_response(**kwargs):
        yield "data: {\"type\": \"summary\", \"content\": \"ok\"}\n\n"

    monkeypatch.setattr(chat_route, "generate_response", fake_generate_response)

    request = _build_request()
    body = ChatRequest(
        messages=[{"role": "user", "content": "正常问题"}],
        session_id="sess-1",
    )

    resp = await chat_route.chat(request, body, {"user_id": "u1"})
    events = []
    async for chunk in resp.body_iterator:
        events.append(chunk)

    parsed = [json.loads(e[6:].strip()) for e in events]
    assert [p["type"] for p in parsed] == ["session_ready", "summary"]


# ---- generate_response：final_output 检测 ----

@pytest.mark.asyncio
async def test_generate_response_output_blocked(monkeypatch):
    """编排输出命中：发送 message_replace，跳过推荐问题，持久化仍执行。"""
    import app.services.chat_service as chat_service_module
    from app.services.chat_service import generate_response

    async def fake_check(text, stage="input"):
        if stage == "output":
            return {
                "blocked": True, "reason": "暴力伤害意图", "category": "violence",
                "sensitive_words": [], "source": "model", "raw": {},
            }
        return {
            "blocked": False, "reason": "", "category": "",
            "sensitive_words": [], "source": "model", "raw": {},
        }

    monkeypatch.setattr(chat_service_module, "check_sensitive", fake_check)

    # 推荐问题标记生成器：若被调用，事件流中会出现 recommended_questions
    async def fake_emit_rq(*args, **kwargs):
        yield "data: {\"type\": \"recommended_questions\", \"questions\": [\"q1\"]}\n\n"

    monkeypatch.setattr(
        chat_service_module, "_emit_recommended_questions", fake_emit_rq
    )

    orch = MagicMock()

    async def fake_run(*args, **kwargs):
        yield "data: {\"type\": \"summary\", \"content\": \"敏感内容\"}\n\n"

    orch.run = fake_run

    session_service = MagicMock()
    session_service.load_messages = AsyncMock(return_value=[])
    session_service.append_messages = AsyncMock()
    session_service.save_latest_trace_id = AsyncMock()

    events = []
    async for ev in generate_response(
        orchestrator_service=orch,
        messages=[{"role": "user", "content": "正常问题"}],
        session_id="sess-1",
        user_id="u1",
        session_service=session_service,
        langfuse_service=None,
    ):
        events.append(ev)

    parsed = [json.loads(e[6:].strip()) for e in events if e.startswith("data: ")]
    types = [p["type"] for p in parsed]

    # 输出命中：message_replace 出现且 stage=output
    assert "message_replace" in types
    replace_ev = next(p for p in parsed if p["type"] == "message_replace")
    assert replace_ev["stage"] == "output"
    assert replace_ev["reason"] == "暴力伤害意图"
    # 跳过推荐问题
    assert "recommended_questions" not in types
    # 持久化仍执行（用户 + assistant 消息）
    session_service.append_messages.assert_called_once()
    persisted = session_service.append_messages.call_args[0][2]
    assert len(persisted) == 2


@pytest.mark.asyncio
async def test_generate_response_output_safe(monkeypatch):
    """编排输出未命中：正常生成推荐问题。"""
    import app.services.chat_service as chat_service_module
    from app.services.chat_service import generate_response

    async def fake_check(text, stage="input"):
        return {
            "blocked": False, "reason": "", "category": "",
            "sensitive_words": [], "source": "model", "raw": {},
        }

    monkeypatch.setattr(chat_service_module, "check_sensitive", fake_check)

    async def fake_emit_rq(*args, **kwargs):
        yield "data: {\"type\": \"recommended_questions\", \"questions\": [\"q1\"]}\n\n"

    monkeypatch.setattr(
        chat_service_module, "_emit_recommended_questions", fake_emit_rq
    )

    orch = MagicMock()

    async def fake_run(*args, **kwargs):
        yield "data: {\"type\": \"summary\", \"content\": \"正常回答\"}\n\n"

    orch.run = fake_run

    session_service = MagicMock()
    session_service.load_messages = AsyncMock(return_value=[])
    session_service.append_messages = AsyncMock()
    session_service.save_latest_trace_id = AsyncMock()

    events = []
    async for ev in generate_response(
        orchestrator_service=orch,
        messages=[{"role": "user", "content": "正常问题"}],
        session_id="sess-1",
        user_id="u1",
        session_service=session_service,
        langfuse_service=None,
    ):
        events.append(ev)

    parsed = [json.loads(e[6:].strip()) for e in events if e.startswith("data: ")]
    types = [p["type"] for p in parsed]
    assert "message_replace" not in types
    assert "recommended_questions" in types
