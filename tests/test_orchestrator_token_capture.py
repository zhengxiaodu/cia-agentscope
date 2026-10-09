"""token 拦截持久化单测：orchestrator 事件流旁路累积 + chat_service 真实值落库。

覆盖三块：
- _capture_tokens_from_stream：MODEL_CALL_END 累积、事件透传、异常容错
- run()：每轮入口重置计数器（单例跨请求复用）
- _persist_conversation_history：assistant.tokens 优先真实值，0 回退估算
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentscope.event import ModelCallEndEvent, ModelCallStartEvent

from app.services.orchestrator_service import OrchestratorService
from app.services.chat_service import _persist_conversation_history


def _sse(payload: dict) -> str:
    """模拟 agentscope 事件在流中的 SSE 字符串形态。"""
    import json
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _model_end_event(reply_id: str, it: int, ot: int) -> str:
    """真实链路形态：ModelCallEndEvent.model_dump_json 前缀 data: 。"""
    return "data: " + ModelCallEndEvent(
        reply_id=reply_id, input_tokens=it, output_tokens=ot,
    ).model_dump_json() + "\n\n"


def _make_service(input_tokens: int = 0, output_tokens: int = 0):
    """绕过 __init__ 的最小实例，只填拦截/属性所需状态。"""
    svc = OrchestratorService.__new__(OrchestratorService)
    svc._last_input_tokens = input_tokens
    svc._last_output_tokens = output_tokens
    return svc


async def _agen(events):
    for ev in events:
        yield ev


# ---------------------------------------------------------------------------
# _capture_tokens_from_stream
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_accumulates_model_call_end():
    svc = _make_service()
    events = [
        _model_end_event("r1", 100, 40),
        _model_end_event("r2", 7, 3),
    ]
    out = [ev async for ev in svc._capture_tokens_from_stream(_agen(events))]
    assert svc._last_input_tokens == 107
    assert svc._last_output_tokens == 43
    assert out == events  # 原样透传


@pytest.mark.asyncio
async def test_capture_ignores_non_model_events():
    """MODEL_CALL_START 及其他事件不含 MODEL_CALL_END 子串，不解析不计数。"""
    svc = _make_service()
    events = [
        "data: " + ModelCallStartEvent(reply_id="r1", model_name="m").model_dump_json() + "\n\n",
        _sse({"type": "TEXT_BLOCK_DELTA", "text": "hi"}),
        _sse({"type": "reply_end"}),
    ]
    async for _ in svc._capture_tokens_from_stream(_agen(events)):
        pass
    assert svc._last_input_tokens == 0
    assert svc._last_output_tokens == 0


@pytest.mark.asyncio
async def test_capture_malformed_event_tolerated():
    """含 MODEL_CALL_END 子串但 JSON 非法：静默容错，不计数、不断流。"""
    svc = _make_service()
    events = [
        "data: {bad json MODEL_CALL_END\n\n",
        _model_end_event("r1", 10, 5),
    ]
    out = [ev async for ev in svc._capture_tokens_from_stream(_agen(events))]
    assert len(out) == 2  # 坏事件也透传，后续事件正常
    assert svc._last_input_tokens == 10
    assert svc._last_output_tokens == 5


@pytest.mark.asyncio
async def test_capture_wrong_type_field_not_counted():
    """子串命中但 type 不是 MODEL_CALL_END：不计数。"""
    svc = _make_service()
    events = [_sse({"type": "MODEL_CALL_END_LIKE", "input_tokens": 999})]
    async for _ in svc._capture_tokens_from_stream(_agen(events)):
        pass
    assert svc._last_input_tokens == 0


@pytest.mark.asyncio
async def test_capture_missing_token_fields_defaults_zero():
    """字段缺失按 0 计，不抛异常。"""
    svc = _make_service()
    events = [_sse({"type": "MODEL_CALL_END"})]
    async for _ in svc._capture_tokens_from_stream(_agen(events)):
        pass
    assert svc._last_input_tokens == 0
    assert svc._last_output_tokens == 0


# ---------------------------------------------------------------------------
# run() 入口重置
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_resets_token_counters():
    """run() 每次调用先清零（单例跨请求复用，避免上一轮残留）。

    用空 messages 走提前返回路径：重置发生在 user_input 校验之前。
    """
    svc = _make_service(input_tokens=999, output_tokens=888)
    consumed = [ev async for ev in svc.run([])]
    assert svc._last_input_tokens == 0
    assert svc._last_output_tokens == 0
    assert any("未检测到有效用户输入" in ev for ev in consumed)


# ---------------------------------------------------------------------------
# _persist_conversation_history：真实 token 落库 + 降级
# ---------------------------------------------------------------------------

def _fake_orchestrator(it: int, ot: int):
    return SimpleNamespace(
        last_agent_ids=["general_agent"],
        last_success=True,
        last_input_tokens=it,
        last_output_tokens=ot,
    )


def _session_service_capture():
    svc = MagicMock()
    svc.append_messages = AsyncMock(return_value={
        "user_message_id": 101, "assistant_message_id": 102,
    })
    return svc


_MESSAGES = [{"role": "user", "content": "总结这份文件"}]
# "回答内容" 4 个中文字符 → 估算 4*1.5 = 6
_ESTIMATE_ANSWER = 6


def _assistant_msg(session_service) -> dict:
    calls = session_service.append_messages.await_args
    new_messages = calls.args[2]
    return next(m for m in new_messages if m["role"] == "assistant")


@pytest.mark.asyncio
async def test_persist_uses_real_tokens():
    session_service = _session_service_capture()
    await _persist_conversation_history(
        _fake_orchestrator(120, 30), session_service,
        "s1", "u1", _MESSAGES, "回答内容",
    )
    assert _assistant_msg(session_service)["tokens"] == 150


@pytest.mark.asyncio
async def test_persist_falls_back_to_estimate_when_zero():
    """真实 token 为 0（自有链路/降级）时回退文本估算。"""
    session_service = _session_service_capture()
    await _persist_conversation_history(
        _fake_orchestrator(0, 0), session_service,
        "s1", "u1", _MESSAGES, "回答内容",
    )
    assert _assistant_msg(session_service)["tokens"] == _ESTIMATE_ANSWER


@pytest.mark.asyncio
async def test_persist_falls_back_when_orchestrator_none():
    """orchestrator_service 为 None（异常兜底路径）时同样回退估算。"""
    session_service = _session_service_capture()
    await _persist_conversation_history(
        None, session_service, "s1", "u1", _MESSAGES, "回答内容",
    )
    assert _assistant_msg(session_service)["tokens"] == _ESTIMATE_ANSWER


@pytest.mark.asyncio
async def test_persist_tolerates_broken_orchestrator():
    """orchestrator 读取属性抛异常：tokens 回退估算，不中断持久化。"""
    class _Broken:
        @property
        def last_agent_ids(self):
            raise RuntimeError("boom")

        @property
        def last_success(self):
            return True

        @property
        def last_input_tokens(self):
            raise RuntimeError("boom")

        @property
        def last_output_tokens(self):
            return 0

    session_service = _session_service_capture()
    await _persist_conversation_history(
        _Broken(), session_service, "s1", "u1", _MESSAGES, "回答内容",
    )
    assert _assistant_msg(session_service)["tokens"] == _ESTIMATE_ANSWER
