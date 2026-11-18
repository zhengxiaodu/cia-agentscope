"""检测结果决定是否更新下一轮将使用的各 agent 状态。"""
import pytest

import app.services.chat_service as chat_service_module
from app.services.chat_service import generate_response
from tests.test_error_fallback import (
    _BLOCKED_SENS, _SAFE_SENS, _collect, _make_orch, _make_request,
    _make_session_service, _patch_recommended_questions, _patch_sensitive,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "block", "cancel", "outage"])
async def test_parallel_agent_states_wait_for_output_decision(monkeypatch, decision):
    """两份状态只包含局部回答。检查在检测期间、放行后以及被停止时的存储内容。"""
    import asyncio

    _patch_sensitive(monkeypatch)
    _patch_recommended_questions(monkeypatch)
    service = _make_session_service()
    stored = {"agent_a": {"summary": "previous round"}}
    approved = asyncio.Event()
    entered = asyncio.Event()

    async def save(sid, uid, aid, snapshot):
        assert approved.is_set()
        stored[aid] = snapshot

    service.save_agent_state.side_effect = save
    async def check(text, stage="output"):
        entered.set()
        await approved.wait()
        if decision == "outage":
            return dict(_SAFE_SENS, source="client-fallback")
        return dict(_BLOCKED_SENS if decision == "block" else _SAFE_SENS)
    monkeypatch.setattr(chat_service_module, "check_sensitive", check)

    states = {"agent_a": {"summary": "part A"}, "agent_b": {"summary": "part B"}}
    orch = _make_orch([])
    async def run(*args, round_state, **kwargs):
        round_state.final_states.update(states)
        yield 'data: {"type":"summary","content":"combined result"}\n\n'
    orch.run = run

    task = asyncio.create_task(_collect(generate_response(
        orchestrator_service=orch, messages=[{"role": "user", "content": "question"}],
        session_id="s1", user_id="u1", session_service=service, langfuse_service=None,
        request=_make_request(upload_dao=None),
    )))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        service.save_agent_state.assert_not_awaited()
        if decision == "cancel":
            task.cancel()
        else:
            approved.set()
        await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    if decision == "allow":
        assert stored == states
        assert service.save_agent_state.await_count == 2
    else:
        assert stored == {"agent_a": {"summary": "previous round"}}
        service.save_agent_state.assert_not_awaited()
