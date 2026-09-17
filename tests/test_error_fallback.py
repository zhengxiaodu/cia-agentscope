"""编排 error 事件前端兜底测试。

覆盖 chat_service.generate_response：
1. 编排流 error 事件 → 前端收到替换后的友好文案，原始错误不透出
2. 非错误事件原样透传（不受拦截逻辑影响）
3. 报错轮无任何输出 → 落库 assistant 内容为友好文案（保证历史不空白）
4. error 后仍有 summary → 落库内容为真实输出（不覆盖为兜底文案）
5. bocha_sum 事件累积随 assistant 消息落库（与 citations 同机制）
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_service as chat_service_module
from app.services.chat_service import (
    _FRIENDLY_ERROR_MESSAGE,
    generate_response,
)

# ---------------------------------------------------------------------------
# 公共 mock（风格对齐 test_message_pair_and_citations.py）
# ---------------------------------------------------------------------------

_SAFE_SENS = {
    "blocked": False, "reason": "", "hit_sources": [],
    "sensitive_words": [], "source": "server", "raw": {},
}


def _patch_sensitive(monkeypatch):
    """屏蔽真实敏感检测外呼。"""
    async def fake_dict(text, stage="input"):
        return dict(_SAFE_SENS)

    async def fake_check(text, stage="output"):
        return dict(_SAFE_SENS)

    monkeypatch.setattr(chat_service_module, "dict_check_sensitive", fake_dict)
    monkeypatch.setattr(chat_service_module, "check_sensitive", fake_check)


def _patch_recommended_questions(monkeypatch):
    """屏蔽推荐问题真实 LLM 外呼。"""
    async def fake_emit_rq(*args, **kwargs):
        yield "data: {\"type\": \"recommended_questions\", \"questions\": [\"q1\"]}\n\n"

    monkeypatch.setattr(chat_service_module, "_emit_recommended_questions", fake_emit_rq)


def _make_orch(events):
    """orchestrator mock：run 为依次 yield 指定事件的 async 生成器。"""
    orch = MagicMock()

    async def fake_run(*args, **kwargs):
        for ev in events:
            yield ev

    orch.run = fake_run
    return orch


def _make_session_service():
    svc = MagicMock()
    svc.load_messages = AsyncMock(return_value=[])
    svc.append_messages = AsyncMock(return_value={
        "user_message_id": 101, "assistant_message_id": 102,
    })
    svc.save_latest_trace_id = AsyncMock()
    svc.append_session_files = AsyncMock()
    svc.mark_last_assistant_failed = AsyncMock()
    return svc


def _make_request(upload_dao=None):
    request = MagicMock()
    request.app.state.upload_file_dao = upload_dao
    return request


async def _collect(gen):
    return [ev async for ev in gen]


def _parse_events(events):
    return [
        json.loads(e[6:].strip()) for e in events if e.startswith("data: ")
    ]


async def _run_generate(monkeypatch, events, upload_dao=None):
    """公共执行器：跑完 generate_response，返回 (前端事件列表, session_service)。"""
    _patch_sensitive(monkeypatch)
    _patch_recommended_questions(monkeypatch)

    orch = _make_orch(events)
    session_service = _make_session_service()
    raw = await _collect(generate_response(
        orchestrator_service=orch,
        messages=[{"role": "user", "content": "问题"}],
        session_id="sess-1",
        user_id="u1",
        session_service=session_service,
        langfuse_service=None,
        request=_make_request(upload_dao=upload_dao),
    ))
    return _parse_events(raw), session_service


# ---------------------------------------------------------------------------
# error 事件前端兜底
# ---------------------------------------------------------------------------

def test_friendly_error_message_constant():
    """兜底文案与需求一致（作为前后端契约基线）。"""
    assert _FRIENDLY_ERROR_MESSAGE == "抱歉，暂时无法生成回复。请重试并联系管理员"


@pytest.mark.asyncio
async def test_error_event_replaced_with_friendly_message(monkeypatch):
    """编排 error 事件 → 前端收到友好文案，原始错误信息不透出。"""
    events, _ = await _run_generate(monkeypatch, [
        'data: {"type": "error", "message": "LLM provider 500 Internal Server Error"}\n\n',
    ])

    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    assert error_events[0]["message"] == _FRIENDLY_ERROR_MESSAGE
    # 原始错误信息不得出现在任何前端事件里
    assert not any("Internal Server Error" in json.dumps(e, ensure_ascii=False)
                   for e in events)


@pytest.mark.asyncio
async def test_non_error_events_pass_through(monkeypatch):
    """非错误事件（summary 等）原样透传，文案不被篡改。"""
    events, _ = await _run_generate(monkeypatch, [
        'data: {"type": "TEXT_BLOCK_DELTA", "delta": "部分输出"}\n\n',
        'data: {"type": "summary", "content": "完整答案"}\n\n',
    ])

    types = [e["type"] for e in events]
    assert "TEXT_BLOCK_DELTA" in types
    assert "summary" in types
    summary = next(e for e in events if e["type"] == "summary")
    assert summary["content"] == "完整答案"


@pytest.mark.asyncio
async def test_error_round_without_output_persists_friendly_message(monkeypatch):
    """报错轮无 summary/兜底输出 → 落库 assistant 内容为友好文案。"""
    _, session_service = await _run_generate(monkeypatch, [
        'data: {"type": "error", "message": "编排失败"}\n\n',
    ])

    session_service.append_messages.assert_awaited_once()
    persisted = session_service.append_messages.await_args[0][2]
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["content"] == _FRIENDLY_ERROR_MESSAGE


@pytest.mark.asyncio
async def test_error_round_with_summary_persists_real_output(monkeypatch):
    """error 后仍有 summary → 落库真实输出（兜底文案不覆盖正常内容）。"""
    _, session_service = await _run_generate(monkeypatch, [
        'data: {"type": "error", "message": "非致命错误"}\n\n',
        'data: {"type": "summary", "content": "已生成的答案"}\n\n',
    ])

    persisted = session_service.append_messages.await_args[0][2]
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["content"] == "已生成的答案"


@pytest.mark.asyncio
async def test_pipeline_intercept_still_preferred_over_error_fallback(monkeypatch):
    """pipeline_intercept 兜底文本优先于 error 兜底文案（既有语义不变）。"""
    _, session_service = await _run_generate(monkeypatch, [
        'data: {"type": "pipeline_intercept", "message": "流程被拦截，原因见详情"}\n\n',
        'data: {"type": "error", "message": "后续步骤失败"}\n\n',
    ])

    persisted = session_service.append_messages.await_args[0][2]
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["content"] == "流程被拦截，原因见详情"


@pytest.mark.asyncio
async def test_normal_round_without_error_keeps_empty_behavior(monkeypatch):
    """无 error 事件且无输出 → 不启用兜底文案（维持原有空输出语义：
    无 final_output 时不追加 assistant 消息）。"""
    _, session_service = await _run_generate(monkeypatch, [
        'data: {"type": "TEXT_BLOCK_DELTA", "delta": "流式片段但不形成 summary"}\n\n',
    ])

    persisted = session_service.append_messages.await_args[0][2]
    # 空输出且无 error → 不落 assistant 消息（原有语义）
    assert not any(m["role"] == "assistant" for m in persisted)


# ---------------------------------------------------------------------------
# bocha_sum 事件收集与落库（与 citations 同机制）
# ---------------------------------------------------------------------------

_BOCHA = {"name": "标题一", "url": "https://example.com/1", "snippet": "片段"}


@pytest.mark.asyncio
async def test_bocha_sum_events_collected_and_persisted(monkeypatch):
    """多条 bocha_sum 事件累积合并随 assistant 消息落库。"""
    _, session_service = await _run_generate(monkeypatch, [
        'data: {"type": "bocha_sum", "bocha_sum": [%s]}\n\n'
        % json.dumps(_BOCHA, ensure_ascii=False),
        'data: {"type": "bocha_sum", "bocha_sum": ['
        '{"name": "标题二", "url": "https://example.com/2", "snippet": "片段2"}]}\n\n',
        'data: {"type": "summary", "content": "基于搜索的回答"}\n\n',
    ])

    persisted = session_service.append_messages.await_args[0][2]
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["bocha_sum"] == [
        _BOCHA,
        {"name": "标题二", "url": "https://example.com/2", "snippet": "片段2"},
    ]
    # user 消息不带 bocha_sum 键（仅 assistant 携带）
    user_msg = next(m for m in persisted if m["role"] == "user")
    assert "bocha_sum" not in user_msg


@pytest.mark.asyncio
async def test_bocha_sum_empty_when_no_events(monkeypatch):
    """无 bocha_sum 事件时 assistant 消息 bocha_sum 为 None（空列表不落库）。"""
    _, session_service = await _run_generate(monkeypatch, [
        'data: {"type": "summary", "content": "普通回答"}\n\n',
    ])

    persisted = session_service.append_messages.await_args[0][2]
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["bocha_sum"] is None
