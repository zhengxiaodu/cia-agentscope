"""Full generate_response cancellation integration tests (requires app SDKs).

Per-round Redis keys prevent a stale stop from aborting a later round.
Route ownership, cross-Pod stop, cleanup and duplicate-request behavior are
covered by test_chat_multireplica.py; this file exercises event throttling
and interrupted conversation persistence through the real generator.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.chat_service as chat_service_module
from app.models.chat import ChatRequest
from app.routes.chat import StopRequest, chat, stop_chat
from app.services.chat_service import generate_response

_SAFE_SENS = {
    "blocked": False, "reason": "", "hit_sources": [],
    "sensitive_words": [], "source": "server", "raw": {},
}


def _patch_sensitive(monkeypatch):
    """屏蔽真实敏感检测外呼（.env 可能配置了真实服务地址）。"""
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
    """orchestrator mock：run 为依次 yield 指定事件的 async 生成器。

    _input_probe_client 置 None → 输入 403 预检无客户端直接放行，不外呼。
    """
    orch = MagicMock()
    orch._input_probe_client = None
    orch._intent_client = None

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


def _make_request(redis=None):
    """generate_response 的 request mock：redis_client 必须显式赋值
    （MagicMock 自动属性非 None 会误入 Redis 分支）。"""
    request = MagicMock()
    request.headers = {}
    request.app.state.redis_client = redis
    request.app.state.upload_file_dao = None
    return request


def _parse_payloads(events):
    return [json.loads(e[6:].strip()) for e in events if e.startswith("data: ")]


def _parse_types(events):
    return [p["type"] for p in _parse_payloads(events)]


def _drive(orch, redis, cancel_event=None, session_service=None):
    """最小化驱动 generate_response：mock 编排流 / 会话服务 / 敏感检测，
    收集全部 SSE 事件。"""
    return generate_response(
        orchestrator_service=orch,
        messages=[{"role": "user", "content": "你好"}],
        session_id="sess-1",
        user_id="u1",
        session_service=session_service or _make_session_service(),
        langfuse_service=None,
        request=_make_request(redis),
        cancel_event=cancel_event,
        workspace_manager=None,
        run_id="run-1",
    )


# Route, ownership and cleanup regressions live in test_chat_multireplica.py.
# This file keeps full generate_response integration tests with app SDKs installed.

# ---- generate_response._cancelled：本地优先 / Redis 节流轮询 / 跨 Pod 中断 ----

@pytest.mark.asyncio
async def test_cancelled_throttled_redis_poll(monkeypatch):
    """Redis 轮询节流：2 个事件检查点 + 编排流结束检查点共 3 次 _cancelled()，
    同一秒（节流窗口）内只真实查询 Redis 一次，流正常收尾不中断。"""
    _patch_sensitive(monkeypatch)
    _patch_recommended_questions(monkeypatch)

    redis = AsyncMock()
    redis.exists.return_value = 0  # 停止标志不存在

    orch = _make_orch([
        'data: {"type": "summary", "content": "片段1"}\n\n',
        'data: {"type": "summary", "content": "片段2"}\n\n',
    ])
    events = [ev async for ev in _drive(orch, redis)]

    # 3 个取消检查点命中同一节流窗口 → exists 只被真实调用一次
    redis.exists.assert_awaited_once_with("chat_stop:sess-1:run-1")
    types = _parse_types(events)
    assert "user_abort" not in types        # 未中断
    assert types.count("summary") == 2      # 两个事件均正常透传
    # 正常轮由 archive_files 下发恰好一次绑定事件（不能被中断兜底再补一条）
    assert types.count("files_generated") == 1


@pytest.mark.asyncio
async def test_cancelled_redis_flag_aborts_stream(monkeypatch):
    """Redis 停止标志存在（其他 Pod /chat/stop 写入）→ _cancelled() 为 True
    → 主动 CancelledError：编排事件不再消费，收尾仍落库并标记失败，
    exists 首查即命中（只查一次）。"""
    _patch_sensitive(monkeypatch)
    _patch_recommended_questions(monkeypatch)

    redis = AsyncMock()
    redis.exists.return_value = 1

    orch = _make_orch([
        'data: {"type": "summary", "content": "片段1"}\n\n',
        'data: {"type": "summary", "content": "不应被消费"}\n\n',
    ])
    session_service = _make_session_service()
    events = [ev async for ev in _drive(orch, redis, session_service=session_service)]

    redis.exists.assert_awaited_once_with("chat_stop:sess-1:run-1")
    types = _parse_types(events)
    assert "user_abort" in types
    assert types.count("summary") == 0  # 命中检查点后事件不再透传
    # Redis 停止标志来自 /chat/stop → 归因为用户主动停止，文案保持“用户已停止”
    abort = [p for p in _parse_payloads(events) if p["type"] == "user_abort"][-1]
    assert abort["reason"] == "user"
    assert abort["message"] == "用户已停止"
    # 中断轮收尾：仍落库 + 强制标记最新 assistant 消息失败
    session_service.append_messages.assert_awaited_once()
    session_service.mark_last_assistant_failed.assert_awaited_once_with("sess-1", "u1")


@pytest.mark.asyncio
async def test_cancelled_local_event_skips_redis(monkeypatch):
    """本地 cancel_event 已置位（同 Pod /chat/stop）→ _cancelled 直接 True，
    不查 Redis（exists 不被调用）。"""
    _patch_sensitive(monkeypatch)
    _patch_recommended_questions(monkeypatch)

    redis = AsyncMock()
    cancel_event = asyncio.Event()
    cancel_event.set()

    orch = _make_orch(['data: {"type": "summary", "content": "x"}\n\n'])
    session_service = _make_session_service()
    events = [ev async for ev in _drive(orch, redis, cancel_event=cancel_event,
                                        session_service=session_service)]

    redis.exists.assert_not_called()
    types = _parse_types(events)
    assert "user_abort" in types
    session_service.mark_last_assistant_failed.assert_awaited_once_with("sess-1", "u1")


# ---- 中断轮兜底落库：保证 assistant 行存在（前端可点赞点踩） ----

@pytest.mark.asyncio
async def test_aborted_round_persists_assistant_fallback_when_no_output(monkeypatch):
    """用户在编排层产出任何内容前中断：仍要落一条非空 assistant 消息。

    否则该轮 message_pair_id 在库中只有 user 行、没有 assistant 行，
    /feedback 按 (session_id, message_pair_id, role='assistant') 命中 0 行 → 404。
    """
    _patch_sensitive(monkeypatch)
    cancel_event = asyncio.Event()
    cancel_event.set()
    orch = _make_orch([])  # 编排层零输出
    session_service = _make_session_service()
    events = [ev async for ev in _drive(orch, None, cancel_event=cancel_event,
                                        session_service=session_service)]

    assert _parse_types(events)[-1] == "user_abort"
    session_service.append_messages.assert_awaited_once()
    persisted = session_service.append_messages.await_args.args[2]
    assistant = [m for m in persisted if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"].strip()   # 正文非空（兜底文案）
    assert assistant[0]["success"] is False


@pytest.mark.asyncio
async def test_aborted_round_persists_streamed_text(monkeypatch):
    """用户在一段正文流式产出后中断：落库正文保留已产出的内容（不被兜底覆盖）。"""
    _patch_sensitive(monkeypatch)
    cancel_event = asyncio.Event()
    orch = _make_orch([])

    async def run(*args, **kwargs):
        yield 'data: {"type": "TEXT_BLOCK_DELTA", "delta": "已产出的正文"}\n\n'
        cancel_event.set()

    orch.run = run
    session_service = _make_session_service()
    events = [ev async for ev in _drive(orch, None, cancel_event=cancel_event,
                                        session_service=session_service)]

    assert _parse_types(events)[-1] == "user_abort"
    persisted = session_service.append_messages.await_args.args[2]
    assistant = [m for m in persisted if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert "已产出的正文" in assistant[0]["content"]


@pytest.mark.asyncio
async def test_aborted_round_emits_message_pair_binding(monkeypatch):
    """中断轮必须补发一次带 message_pair_id 的 files_generated（空 files）。

    正常轮靠 archive_files 下发的该事件让前端绑定本轮；中断轮若跳过它，
    刚中断时前端拿不到 message_pair_id，点赞点踩要刷新会话详情才能用。
    """
    _patch_sensitive(monkeypatch)
    cancel_event = asyncio.Event()
    cancel_event.set()
    orch = _make_orch([])
    session_service = _make_session_service()
    events = [ev async for ev in _drive(orch, None, cancel_event=cancel_event,
                                        session_service=session_service)]

    payloads = _parse_payloads(events)
    bindings = [p for p in payloads if p["type"] == "files_generated"]
    assert len(bindings) == 1
    assert bindings[0]["files"] == []
    assert bindings[0]["message_pair_id"]           # 非空，可绑定本轮
    # 绑定事件需早于 user_abort 下发
    assert payloads.index(bindings[0]) < payloads.index(
        next(p for p in payloads if p["type"] == "user_abort")
    )


@pytest.mark.asyncio
async def test_stop_through_heartbeat_emits_binding_to_client(monkeypatch):
    """忠实复现路由层链路：/chat 的 source() 包装 + _with_heartbeat 心跳泵，
    再经 /chat/stop（request_stop）置位本轮取消信号。

    验证：流式过程中用户点击 stop、且本轮未生成文件时，收尾补发的
    files_generated（files=[] + message_pair_id）确实下发到客户端，且早于
    user_abort。用户可见的"点击 stop 后没有绑定事件"若发生，则不在此链路。
    """
    from app.routes.chat import _with_heartbeat
    from app.services.chat_runtime import ChatRun, request_stop

    _patch_sensitive(monkeypatch)
    run = ChatRun(None, "sess-1", "u1")
    await run.open()
    orch = _make_orch([])

    async def run_upstream(*args, **kwargs):
        yield 'data: {"type": "summary", "content": "部分内容"}\n\n'
        await asyncio.Event().wait()   # 静默挂起：模拟 LLM 不再产出
        yield ""

    orch.run = run_upstream
    session_service = _make_session_service()

    async def source():
        # 与 /chat 路由同构：先发 session_ready，再转发 generate_response
        yield f"data: {json.dumps({'type': 'session_ready', 'session_id': 'sess-1'})}\n\n"
        async for ev in generate_response(
            orchestrator_service=orch,
            messages=[{"role": "user", "content": "你好"}],
            session_id="sess-1", user_id="u1",
            session_service=session_service,
            langfuse_service=None,
            request=_make_request(None),
            cancel_event=run.cancel_event,
            workspace_manager=None,
            run_id=run.run_id,
        ):
            yield ev

    events = []

    async def collect():
        async for ev in _with_heartbeat(source(), cancel_event=run.cancel_event):
            events.append(ev)

    # 客户端保持连接；正文流式产出一段后用户点击停止（模拟 /chat/stop）
    task = asyncio.create_task(collect())
    await asyncio.sleep(0.3)
    assert await request_stop(None, "sess-1", "u1", run) is True
    assert run.cancel_event.is_set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise AssertionError("用户停止后 SSE 流未结束（收尾事件未下发）")

    payloads = _parse_payloads(events)
    types = [p["type"] for p in payloads]
    assert "summary" in types        # 已产出的正文先下发
    assert "user_abort" in types
    bindings = [p for p in payloads if p["type"] == "files_generated"]
    assert len(bindings) == 1
    assert bindings[0]["files"] == []
    assert bindings[0]["message_pair_id"]           # 非空，可绑定本轮
    # 绑定事件需早于 user_abort 下发
    assert payloads.index(bindings[0]) < payloads.index(
        next(p for p in payloads if p["type"] == "user_abort")
    )
