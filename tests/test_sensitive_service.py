"""安全敏感内容检测单测：check/dict-check 命中/放行/兜底/开关 + Langfuse 埋点 + chat 入口拦截。"""
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
    dict_check_sensitive,
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
        sensitive_service, "SENSITIVE_SERVICE_URL", "http://fake"
    )


def _hit_response():
    return _MockResponse({
        "code": 0,
        "hasSensitiveWord": True,
        "hitSources": ["dictionary", "semantic"],
        "reason": "触犯暴力危害关键词和语义",
        "sensitiveWords": ["枪杀"],
    })


def _safe_response():
    return _MockResponse({
        "code": 0,
        "hasSensitiveWord": False,
        "hitSources": [],
        "reason": "",
        "sensitiveWords": [],
    })


def _dict_hit_response():
    return _MockResponse({
        "code": 0,
        "hasSensitiveWord": True,
        "sensitiveWords": ["枪杀"],
    })


def _dict_safe_response():
    return _MockResponse({
        "code": 0,
        "hasSensitiveWord": False,
        "sensitiveWords": [],
    })


# ---- check_sensitive 核心逻辑 ----

@pytest.mark.asyncio
async def test_check_sensitive_hit(monkeypatch):
    """命中：code=0 且 hasSensitiveWord=true → blocked=True 及原因/命中来源。"""
    _install_httpx(monkeypatch, response=_hit_response())
    result = await check_sensitive("我要枪杀某人", stage="output")
    assert result["blocked"] is True
    assert result["reason"] == "触犯暴力危害关键词和语义"
    assert result["hit_sources"] == ["dictionary", "semantic"]
    assert result["sensitive_words"] == ["枪杀"]
    assert result["raw"]["code"] == 0
    # 请求体包含文本与阈值；URL 为基础地址 + /sensitive/check
    client = _MockAsyncClient.instances[-1]
    assert client.post_calls[0]["url"] == "http://fake/sensitive/check"
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
    assert upd["output"]["reason"] == "触犯暴力危害关键词和语义"
    assert upd["output"]["hit_sources"] == ["dictionary", "semantic"]
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
        {"reason": "触犯暴力危害关键词和语义", "hit_sources": ["semantic"]}, stage="input"
    )
    assert event["type"] == "message_replace"
    assert event["stage"] == "input"
    assert event["reason"] == "触犯暴力危害关键词和语义"
    assert "触犯暴力危害关键词和语义" in event["message"]
    assert "请修改提问" in event["message"]


def test_build_message_replace_event_without_reason():
    event = build_message_replace_event({"reason": ""}, stage="output")
    assert event["type"] == "message_replace"
    assert event["stage"] == "output"
    assert event["reason"] == ""
    assert event["message"] == "您的内容涉及敏感词，请修改提问"


# ---- /chat 入口：用户输入检测（在 generate_response 根 span 内执行） ----

def _build_request(orch=None):
    request = MagicMock()
    request.headers = {}
    session_service = MagicMock()
    session_service.get_or_create_session = AsyncMock(return_value="sess-1")
    session_service.load_messages = AsyncMock(return_value=[])
    session_service.append_messages = AsyncMock()
    session_service.save_latest_trace_id = AsyncMock()
    request.app.state.session_service = session_service
    request.app.state.chat_tasks = {}
    request.app.state.langfuse_service = None
    request.app.state.workspace_backend = "docker"
    request.app.state.workspace_manager = None
    request.app.state.upload_file_dao = None
    request.app.state.orchestrator_service = orch if orch is not None else MagicMock()
    return request


_SAFE_DICT_RESULT = {
    "blocked": False, "reason": "", "hit_sources": [],
    "sensitive_words": [], "source": "server", "raw": {},
}


def _make_orch(summary_content="hello", run_calls=None):
    """构造 orchestrator mock：run 为 yield summary 的 async 生成器。

    run_calls 传入 dict 时统计 run 被调用次数（便于断言"未进入编排"）。
    """
    orch = MagicMock()

    async def fake_run(*args, **kwargs):
        if run_calls is not None:
            run_calls["n"] += 1
        yield f"data: {{\"type\": \"summary\", \"content\": \"{summary_content}\"}}\n\n"

    orch.run = fake_run
    return orch


def _patch_output_side_effects(monkeypatch, dict_result=None, check_result=None):
    """屏蔽 generate_response 后续阶段的真实外呼（输出检测/推荐问题）。"""
    import app.services.chat_service as chat_service_module

    async def fake_dict(text, stage="input"):
        return dict_result if dict_result is not None else dict(_SAFE_DICT_RESULT)

    async def fake_check(text, stage="output"):
        return check_result if check_result is not None else dict(_SAFE_DICT_RESULT)

    monkeypatch.setattr(chat_service_module, "dict_check_sensitive", fake_dict)
    monkeypatch.setattr(chat_service_module, "check_sensitive", fake_check)

    async def fake_emit_rq(*args, **kwargs):
        yield "data: {\"type\": \"recommended_questions\", \"questions\": [\"q1\"]}\n\n"

    monkeypatch.setattr(chat_service_module, "_emit_recommended_questions", fake_emit_rq)


@pytest.mark.asyncio
async def test_chat_input_blocked_emits_message_replace(monkeypatch):
    """用户输入命中（词典检测，在 generate_response 根 span 内）：
    发送 message_replace 事件，编排主流程不被执行。"""
    from app.models.chat import ChatRequest
    from app.routes import chat as chat_route

    _patch_output_side_effects(
        monkeypatch,
        dict_result={
            "blocked": True, "reason": "命中敏感词：枪杀",
            "hit_sources": ["dictionary"], "sensitive_words": ["枪杀"],
            "source": "server", "raw": {},
        },
    )

    run_calls = {"n": 0}
    orch = _make_orch(run_calls=run_calls)

    request = _build_request(orch)
    body = ChatRequest(
        messages=[{"role": "user", "content": "我要枪杀某人"}],
        session_id="sess-1",
    )

    resp = await chat_route.chat(request, body, {"user_id": "u1"})
    events = []
    async for chunk in resp.body_iterator:
        events.append(chunk)

    # 不进入编排主流程
    assert run_calls["n"] == 0
    # 事件序列：session_ready → message_replace（→ trace_ready 收尾）
    parsed = [json.loads(e[6:].strip()) for e in events]
    assert [p["type"] for p in parsed][:2] == ["session_ready", "message_replace"]
    replace_ev = next(p for p in parsed if p["type"] == "message_replace")
    assert replace_ev["stage"] == "input"
    assert replace_ev["reason"] == "命中敏感词：枪杀"
    assert "敏感" in replace_ev["message"]
    # 流结束后注册表清理
    assert "sess-1" not in request.app.state.chat_tasks


@pytest.mark.asyncio
async def test_chat_input_safe_calls_generate_response(monkeypatch):
    """用户输入未命中：正常进入编排主流程。"""
    from app.models.chat import ChatRequest
    from app.routes import chat as chat_route

    _patch_output_side_effects(monkeypatch)

    run_calls = {"n": 0}
    orch = _make_orch(summary_content="hello", run_calls=run_calls)
    request = _build_request(orch)
    body = ChatRequest(
        messages=[{"role": "user", "content": "今天天气很好"}],
        session_id="sess-1",
    )

    resp = await chat_route.chat(request, body, {"user_id": "u1"})
    events = []
    async for chunk in resp.body_iterator:
        events.append(chunk)

    assert run_calls["n"] == 1
    parsed = [json.loads(e[6:].strip()) for e in events]
    assert "message_replace" not in [p["type"] for p in parsed]
    assert "summary" in [p["type"] for p in parsed]


@pytest.mark.asyncio
async def test_chat_input_fallback_continues(monkeypatch):
    """兜底：检测服务异常时放行，主流程不受影响。"""
    from app.models.chat import ChatRequest
    from app.routes import chat as chat_route

    _patch_output_side_effects(
        monkeypatch,
        dict_result={
            "blocked": False, "reason": "timeout", "hit_sources": [],
            "sensitive_words": [], "source": "client-fallback", "raw": None,
        },
    )

    orch = _make_orch(summary_content="ok")
    request = _build_request(orch)
    body = ChatRequest(
        messages=[{"role": "user", "content": "正常问题"}],
        session_id="sess-1",
    )

    resp = await chat_route.chat(request, body, {"user_id": "u1"})
    events = []
    async for chunk in resp.body_iterator:
        events.append(chunk)

    parsed = [json.loads(e[6:].strip()) for e in events]
    types = [p["type"] for p in parsed]
    assert types[0] == "session_ready"
    assert "summary" in types
    assert "message_replace" not in types


# ---- generate_response：final_output 检测 ----

@pytest.mark.asyncio
async def test_generate_response_output_blocked(monkeypatch):
    """编排输出命中：发送 message_replace，跳过推荐问题，持久化仍执行。"""
    import app.services.chat_service as chat_service_module
    from app.services.chat_service import generate_response

    async def fake_check(text, stage="input"):
        if stage == "output":
            return {
                "blocked": True, "reason": "触犯暴力危害关键词和语义",
                "hit_sources": ["dictionary", "semantic"],
                "sensitive_words": [], "source": "server", "raw": {},
            }
        return {
            "blocked": False, "reason": "", "hit_sources": [],
            "sensitive_words": [], "source": "server", "raw": {},
        }

    monkeypatch.setattr(chat_service_module, "check_sensitive", fake_check)

    # 输入词典检测放行（避免真实 HTTP：.env 已加载真实服务地址）
    async def fake_dict_check(text, stage="input"):
        return {
            "blocked": False, "reason": "", "hit_sources": [],
            "sensitive_words": [], "source": "server", "raw": {},
        }

    monkeypatch.setattr(chat_service_module, "dict_check_sensitive", fake_dict_check)

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
    assert replace_ev["reason"] == "触犯暴力危害关键词和语义"
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
            "blocked": False, "reason": "", "hit_sources": [],
            "sensitive_words": [], "source": "server", "raw": {},
        }

    monkeypatch.setattr(chat_service_module, "check_sensitive", fake_check)

    # 输入词典检测放行（避免真实 HTTP：.env 已加载真实服务地址）
    async def fake_dict_check(text, stage="input"):
        return {
            "blocked": False, "reason": "", "hit_sources": [],
            "sensitive_words": [], "source": "server", "raw": {},
        }

    monkeypatch.setattr(chat_service_module, "dict_check_sensitive", fake_dict_check)

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


# ---- dict_check_sensitive：用户输入纯词典检测 ----

@pytest.mark.asyncio
async def test_dict_check_hit(monkeypatch):
    """命中：blocked=True，reason 由客户端构造（列出命中词），hit_sources=["dictionary"]。"""
    _install_httpx(monkeypatch, response=_dict_hit_response())
    result = await dict_check_sensitive("我要枪杀某人", stage="input")
    assert result["blocked"] is True
    assert result["reason"] == "命中敏感词：枪杀"
    assert result["hit_sources"] == ["dictionary"]
    assert result["sensitive_words"] == ["枪杀"]
    assert result["raw"]["code"] == 0
    # 请求体仅 {"text": ...}，不带 threshold；URL 由 check 地址派生
    client = _MockAsyncClient.instances[-1]
    assert client.post_calls[0]["url"] == "http://fake/sensitive/dict-check"
    assert client.post_calls[0]["json"] == {"text": "我要枪杀某人"}


@pytest.mark.asyncio
async def test_dict_check_safe(monkeypatch):
    """未命中：放行且无 reason/hit_sources。"""
    _install_httpx(monkeypatch, response=_dict_safe_response())
    result = await dict_check_sensitive("今天天气很好", stage="input")
    assert result["blocked"] is False
    assert result["reason"] == ""
    assert result["hit_sources"] == []
    assert result["sensitive_words"] == []


@pytest.mark.asyncio
async def test_dict_check_timeout(monkeypatch):
    """兜底：超时 → 放行。"""
    _install_httpx(
        monkeypatch,
        exc=real_httpx.TimeoutException("timed out"),
    )
    result = await dict_check_sensitive("文本", stage="input")
    assert result["blocked"] is False
    assert result["source"] == "client-fallback"


@pytest.mark.asyncio
async def test_dict_check_legacy_url_compat(monkeypatch):
    """兼容：SENSITIVE_SERVICE_URL 配成遗留完整路径（…/sensitive/check）也能正确拼出 dict-check 地址。"""
    _install_httpx(monkeypatch, response=_dict_hit_response())
    monkeypatch.setattr(
        sensitive_service, "SENSITIVE_SERVICE_URL", "http://fake/sensitive/check"
    )
    result = await dict_check_sensitive("我要枪杀某人", stage="input")
    assert result["blocked"] is True
    assert result["reason"] == "命中敏感词：枪杀"
    client = _MockAsyncClient.instances[-1]
    assert client.post_calls[0]["url"] == "http://fake/sensitive/dict-check"


@pytest.mark.asyncio
async def test_check_legacy_dict_check_url_compat(monkeypatch):
    """兼容：SENSITIVE_SERVICE_URL 配成遗留 …/sensitive/dict-check 时 check 接口也能正确拼接。"""
    _install_httpx(monkeypatch, response=_hit_response())
    monkeypatch.setattr(
        sensitive_service, "SENSITIVE_SERVICE_URL", "http://fake/sensitive/dict-check"
    )
    result = await check_sensitive("我要枪杀某人", stage="output")
    assert result["blocked"] is True
    client = _MockAsyncClient.instances[-1]
    assert client.post_calls[0]["url"] == "http://fake/sensitive/check"


@pytest.mark.asyncio
async def test_service_base_strips_trailing_slash(monkeypatch):
    """基础地址尾部斜杠被剥掉，不影响路径拼接。"""
    _install_httpx(monkeypatch, response=_dict_hit_response())
    monkeypatch.setattr(sensitive_service, "SENSITIVE_SERVICE_URL", "http://fake/")
    result = await dict_check_sensitive("我要枪杀某人", stage="input")
    assert result["blocked"] is True
    client = _MockAsyncClient.instances[-1]
    assert client.post_calls[0]["url"] == "http://fake/sensitive/dict-check"


@pytest.mark.asyncio
async def test_dict_check_empty_text(monkeypatch):
    """兜底：空文本直接放行，不发请求。"""
    _install_httpx(monkeypatch, response=_dict_hit_response())
    result = await dict_check_sensitive("   ", stage="input")
    assert result["blocked"] is False
    assert _MockAsyncClient.instances == []


# ---- SENSITIVE_ENABLED 总开关 ----

@pytest.mark.asyncio
async def test_sensitive_enabled_false_skips_check(monkeypatch):
    """开关关闭：check_sensitive 与 dict_check_sensitive 均跳过，不发请求。"""
    _install_httpx(monkeypatch, response=_hit_response())
    monkeypatch.setattr(sensitive_service, "SENSITIVE_ENABLED", False)

    result = await check_sensitive("任意文本", stage="output")
    assert result["blocked"] is False
    assert result["source"] == "client-skip"

    result = await dict_check_sensitive("任意文本", stage="input")
    assert result["blocked"] is False
    assert result["source"] == "client-skip"

    assert _MockAsyncClient.instances == []
