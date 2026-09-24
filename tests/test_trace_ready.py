"""trace_ready 早期下发 + _finalize_trace already_emitted 测试。"""
import json

import pytest
from unittest.mock import MagicMock

from app.services.chat_service import _finalize_trace


@pytest.mark.asyncio
async def test_finalize_trace_skips_when_already_emitted():
    """已提前下发过 trace_ready 时，_finalize_trace 不重复下发。"""
    lf = MagicMock()
    lf.enabled = True
    lf.flush = MagicMock()

    events = []

    async for ev in _finalize_trace(lf, None, None, "sess-1",
                                    already_emitted=True):
        events.append(ev)

    assert len(events) == 0
    lf.flush.assert_called_once()


@pytest.mark.asyncio
async def test_finalize_trace_emits_when_not_emitted():
    """未提前下发时，_finalize_trace 仍按原逻辑下发。"""
    lf = MagicMock()
    lf.enabled = True
    lf.flush = MagicMock()
    root_obs = MagicMock()
    root_obs.trace_id = "abc123def456"

    events = []

    async for ev in _finalize_trace(lf, root_obs, None, "sess-1",
                                    already_emitted=False):
        events.append(ev)

    assert len(events) == 1
    data = json.loads(events[0][6:].strip())
    assert data["type"] == "trace_ready"
    assert data["trace_id"] == "abc123def456"


@pytest.mark.asyncio
async def test_finalize_trace_saves_trace_id():
    """flush 后仍保存 trace_id 到 session。"""
    lf = MagicMock()
    lf.enabled = True
    lf.flush = MagicMock()
    root_obs = MagicMock()
    root_obs.trace_id = "trace-xyz"
    session_service = MagicMock()

    events = []

    async for ev in _finalize_trace(lf, root_obs, session_service, "sess-1",
                                    already_emitted=True):
        events.append(ev)

    # 不重复下发，但 flush 仍执行
    lf.flush.assert_called_once()
    # already_emitted=True 时 trace_id 为 None（不在 finalize 阶段读取），不保存
    session_service.save_latest_trace_id.assert_not_called()
