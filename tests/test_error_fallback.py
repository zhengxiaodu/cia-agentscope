"""编排 error 事件前端兜底测试。

覆盖 chat_service.generate_response：
1. 编排流 error 事件 → 前端收到替换后的友好文案，原始错误不透出
2. 非错误事件原样透传（不受拦截逻辑影响）
3. 报错轮无任何输出 → 落库 assistant 内容为友好文案（保证历史不空白）
4. error 后仍有 summary → 落库内容为真实输出（不覆盖为兜底文案）
5. bocha_sum 事件累积随 assistant 消息落库（与 citations 同机制）
6. 大模型 403（状态码/业务 code）→ 前端与落库均为敏感拦截文案
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_service as chat_service_module
from app.services.chat_service import (
    _FRIENDLY_ERROR_MESSAGE,
    generate_response,
)
from app.services.sensitive_service import SENSITIVE_BLOCK_MESSAGE

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
    # 预检围栏客户端置空：既有用例不触发 403 预检外呼（围栏直接放行）
    orch._input_probe_client = None
    orch._input_probe_model_cfg = None

    async def fake_run(*args, **kwargs):
        # 对齐真实编排层契约：执行智能体前汇合输入围栏预检结果，
        # 命中时 yield input_llm_403 标记（chat_service 据此下发拦截文案）
        probe = kwargs.get("input_probe_task")
        if probe is not None and await probe:
            yield 'data: {"type": "input_llm_403"}\n\n'
            return
        for ev in events:
            yield ev

    orch.run = fake_run
    return orch


def _make_probe_orch(events, probe_exc=None):
    """带 403 预检客户端的 orchestrator mock（复用 _make_orch 的事件流）。

    probe_exc=None → 预检调用正常返回（放行）；
    probe_exc=单个异常 → 每次探测都抛该异常（网关级故障：含对照探测）；
    probe_exc=列表 → 按调用次序依次生效（如 [403异常, MagicMock()]
    表示用户输入命中 403、对照探测通过 → 真实内容拦截）。
    """
    orch = _make_orch(events)
    client = MagicMock()
    if probe_exc is not None:
        side = probe_exc if isinstance(probe_exc, list) else probe_exc
        client.chat.completions.create = AsyncMock(side_effect=side)
    else:
        client.chat.completions.create = AsyncMock(return_value=MagicMock())
    orch._input_probe_client = client
    orch._input_probe_model_cfg = {
        "model_name": "deepseek-v4-flash",
        "parameters": {"temperature": 0.1, "max_tokens": 1},
    }
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
    svc.save_agent_state = AsyncMock()
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


async def _run_generate(monkeypatch, events, upload_dao=None, orch=None):
    """公共执行器：跑完 generate_response，返回 (前端事件列表, session_service)。"""
    _patch_sensitive(monkeypatch)
    _patch_recommended_questions(monkeypatch)

    if orch is None:
        orch = _make_orch(events)
    else:
        # Keep the supplied probe client while streaming this test's business events.
        orch.run = _make_orch(events).run
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
# 大模型 403 → 敏感拦截文案
# ---------------------------------------------------------------------------

_LLM_403_MSG = "执行出错: Error code: 403 - {\"code\":403,\"message\":\"content blocked\"}"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_error", [
    "执行出错: Error code: 403 - content blocked",
    "执行出错: Client error '403 Forbidden' for url 'http://llm/v1/chat'",
])
async def test_error_403_replaced_with_sensitive_message(monkeypatch, raw_error):
    """大模型 403 → 前端收到敏感拦截文案，原始错误细节不透出。"""
    events, _ = await _run_generate(monkeypatch, [
        f'data: {json.dumps({"type": "error", "message": raw_error}, ensure_ascii=False)}\n\n',
    ])

    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    assert error_events[0]["message"] == SENSITIVE_BLOCK_MESSAGE
    assert error_events[0]["message"] != _FRIENDLY_ERROR_MESSAGE
    # 原始错误信息不得出现在任何前端事件里
    assert not any("403" in json.dumps(e, ensure_ascii=False) for e in events)


@pytest.mark.asyncio
async def test_error_403_round_persists_sensitive_message(monkeypatch):
    """403 报错轮无输出 → 落库 assistant 内容为敏感拦截文案。"""
    _, session_service = await _run_generate(monkeypatch, [
        f'data: {json.dumps({"type": "error", "message": _LLM_403_MSG}, ensure_ascii=False)}\n\n',
    ])

    session_service.append_messages.assert_awaited_once()
    persisted = session_service.append_messages.await_args[0][2]
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["content"] == SENSITIVE_BLOCK_MESSAGE


@pytest.mark.asyncio
async def test_pipeline_intercept_403_sanitized(monkeypatch):
    """pipeline_intercept 内嵌 403 错误 → 透传事件与落库内容均为敏感文案。"""
    events, session_service = await _run_generate(monkeypatch, [
        'data: {"type": "pipeline_intercept", "message": "步骤 1（i1）执行失败，'
        '流水线终止：执行出错: Error code: 403 - content blocked"}\n\n',
    ])

    intercept = [e for e in events if e.get("type") == "pipeline_intercept"]
    assert len(intercept) == 1
    assert intercept[0]["message"] == SENSITIVE_BLOCK_MESSAGE
    # 原始错误细节不透出
    assert not any("Error code" in json.dumps(e, ensure_ascii=False) for e in events)

    persisted = session_service.append_messages.await_args[0][2]
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["content"] == SENSITIVE_BLOCK_MESSAGE


@pytest.mark.parametrize("message,expected", [
    ("Error code: 403 - blocked", True),
    ("Client error '403 Forbidden' for url", True),
    ("code=403, message=sensitive", True),
    ("Error code: 40300 - other", False),
    ("connection timeout", False),
    ("", False),
])
def test_is_llm_403_error(message, expected):
    """403 判定：词边界匹配，40300 等数字串不误命中。"""
    assert chat_service_module._is_llm_403_error(message) is expected


# ---------------------------------------------------------------------------
# 输入围栏：大模型 403 预检
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_input_probe_403_blocks_before_orchestration(monkeypatch):
    """输入预检命中 403（对照探测通过=真实内容拦截）→ 标准流式事件序列
    下发拦截文案，编排不执行，拦截轮照常落库。"""
    events, session_service = await _run_generate(monkeypatch, [
        'data: {"type": "summary", "content": "不应出现"}\n\n',
    ], orch=_make_probe_orch(
        [], probe_exc=[Exception(_LLM_403_MSG), MagicMock()]
    ))

    # 开始标志（与正常编排流同构）
    starts = [e for e in events if e.get("type") == "orchestration_start"]
    assert len(starts) == 1
    assert starts[0]["mode"] == "sensitive_block"
    # 正文走 pipeline_intercept 事件通道下发拦截文案
    intercepts = [e for e in events if e.get("type") == "pipeline_intercept"]
    assert intercepts and intercepts[0]["message"] == SENSITIVE_BLOCK_MESSAGE
    # task_end 位于 summary 之前
    types = [e.get("type") for e in events]
    assert "task_end" in types
    assert types.index("task_end") < types.index("summary")
    task_end = next(e for e in events if e.get("type") == "task_end")
    assert task_end["intent_id"] == "sensitive_block"
    assert task_end["success"] is True
    # 结束标志：summary，内容为拦截文案
    summaries = [e for e in events if e.get("type") == "summary"]
    assert len(summaries) == 1
    assert summaries[0]["content"] == SENSITIVE_BLOCK_MESSAGE
    # 最后一条业务事件：files_generated；trace_ready 随后由 finalize 发出。
    business_events = [e for e in events if e.get("type") != "trace_ready"]
    assert business_events[-1]["type"] == "files_generated"
    files_ev = business_events[-1]
    assert files_ev["files"] == []
    assert files_ev["message_pair_id"]
    # 输入拦截不再使用 message_replace（输出阶段拦截保留该语义）
    assert not any(e.get("type") == "message_replace" for e in events)
    # 拦截轮照常落库：user=原始输入，assistant=拦截文案
    session_service.append_messages.assert_awaited_once()
    persisted = session_service.append_messages.await_args[0][2]
    user_msg = next(m for m in persisted if m["role"] == "user")
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert user_msg["content"] == "问题"
    assert asst_msg["content"] == SENSITIVE_BLOCK_MESSAGE


@pytest.mark.asyncio
async def test_input_probe_gateway_wide_403_fails_open(monkeypatch):
    """网关级 403（对照探测"你好"同样 403，如配额耗尽/鉴权失效）→
    判定为网关故障，本轮放行，编排正常执行。"""
    events, _ = await _run_generate(monkeypatch, [
        'data: {"type": "summary", "content": "正常回答"}\n\n',
    ], orch=_make_probe_orch([], probe_exc=Exception(_LLM_403_MSG)))

    # 未被拦截：编排 summary 正常下发
    summaries = [e for e in events if e.get("type") == "summary"]
    assert summaries and summaries[0]["content"] == "正常回答"
    # 无拦截事件序列
    assert not any(
        e.get("type") == "orchestration_start"
        and e.get("mode") == "sensitive_block"
        for e in events
    )
    # 对照探测确实发起了两次（用户输入 + "你好"）
    # （_make_probe_orch 的单异常 side_effect 对每次调用都生效）


@pytest.mark.asyncio
async def test_input_probe_non_403_error_fails_open(monkeypatch):
    """预检非 403 异常（超时/网络错误）→ 放行，编排正常执行。"""
    events, _ = await _run_generate(monkeypatch, [
        'data: {"type": "summary", "content": "正常回答"}\n\n',
    ], orch=_make_probe_orch([], probe_exc=TimeoutError("connection timed out")))

    summaries = [e for e in events if e.get("type") == "summary"]
    assert summaries and summaries[0]["content"] == "正常回答"


@pytest.mark.asyncio
async def test_input_probe_pass_continues_flow(monkeypatch):
    """预检通过（网关放行）→ 编排正常执行，无 message_replace。"""
    events, _ = await _run_generate(monkeypatch, [
        'data: {"type": "summary", "content": "正常回答"}\n\n',
    ], orch=_make_probe_orch([]))

    assert any(
        e.get("type") == "summary" and e.get("content") == "正常回答"
        for e in events
    )
    assert not any(e.get("type") == "message_replace" for e in events)


@pytest.mark.asyncio
async def test_input_probe_disabled_skips_call(monkeypatch):
    """开关关闭（LLM_INPUT_PROBE_ENABLED=false）→ 不发起预检调用，直接放行。"""
    monkeypatch.setattr(chat_service_module, "LLM_INPUT_PROBE_ENABLED", False)
    orch = _make_probe_orch([])
    events, _ = await _run_generate(monkeypatch, [
        'data: {"type": "summary", "content": "正常回答"}\n\n',
    ], orch=orch)

    assert any(e.get("type") == "summary" for e in events)
    orch._input_probe_client.chat.completions.create.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# 输出敏感命中：持久化脱敏（P1-4）
# ---------------------------------------------------------------------------

_BLOCKED_SENS = {
    "blocked": True, "reason": "命中敏感词", "hit_sources": ["local"],
    "sensitive_words": ["敏感词"], "source": "server", "raw": {},
}


@pytest.mark.asyncio
async def test_output_sensitive_blocked_persists_sanitized(monkeypatch):
    """输出敏感命中 → message_replace 下发拦截文案，落库内容同步脱敏
    （原文不得进入历史，避免历史回显二次泄露）。"""
    _patch_recommended_questions(monkeypatch)

    async def fake_dict(text, stage="input"):
        return dict(_SAFE_SENS)

    async def fake_check(text, stage="output"):
        return dict(_BLOCKED_SENS)

    monkeypatch.setattr(chat_service_module, "dict_check_sensitive", fake_dict)
    monkeypatch.setattr(chat_service_module, "check_sensitive", fake_check)

    session_service = _make_session_service()
    raw = await _collect(generate_response(
        orchestrator_service=_make_orch([
            'data: {"type": "TEXT_BLOCK_DELTA", "delta": "敏感内容片段"}\n\n',
            'data: {"type": "summary", "content": "包含敏感词的完整回答"}\n\n',
        ]),
        messages=[{"role": "user", "content": "问题"}],
        session_id="sess-1",
        user_id="u1",
        session_service=session_service,
        langfuse_service=None,
        request=_make_request(upload_dao=None),
    ))
    events = _parse_events(raw)

    # message_replace 事件下发（前端替换展示），且跳过推荐问题
    replaces = [e for e in events if e.get("type") == "message_replace"]
    assert len(replaces) == 1
    assert replaces[0]["message"] == SENSITIVE_BLOCK_MESSAGE
    assert not any(e.get("type") == "recommended_questions" for e in events)

    # 落库：assistant 内容为拦截文案，原文不进历史
    session_service.append_messages.assert_awaited_once()
    persisted = session_service.append_messages.await_args[0][2]
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["content"] == SENSITIVE_BLOCK_MESSAGE
    assert "包含敏感词的完整回答" not in json.dumps(persisted, ensure_ascii=False)
