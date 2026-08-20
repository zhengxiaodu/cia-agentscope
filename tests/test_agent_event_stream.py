"""P1-2 iter_agent_events helper 单测：事件透传 + ReplyStart 回调 + tracer 喂入。"""
from unittest.mock import MagicMock

import pytest

from app.utils.agent_event_stream import iter_agent_events


class FakeReplyStart:
    def __init__(self, name, reply_id):
        self.name = name; self.reply_id = reply_id


class FakeAgentEvent:
    def __init__(self, tag):
        self.tag = tag


class FakeAgent:
    def __init__(self, events):
        self._events = events

    async def reply_stream(self, user_msg):
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_yields_agent_events_and_feeds_tracer(monkeypatch):
    import app.utils.agent_event_stream as m
    monkeypatch.setattr(m, "ReplyStartEvent", FakeReplyStart)
    monkeypatch.setattr(m, "AgentEvent", FakeAgentEvent)
    rs = FakeReplyStart("bot", "r1")
    e1 = FakeAgentEvent("a"); e2 = FakeAgentEvent("b")
    agent = FakeAgent([rs, e1, e2])
    tracer = MagicMock()

    started = []

    out = []

    async for ev in iter_agent_events(agent, "msg", tracer, lambda e: started.append(e)):
        out.append(ev)

    assert out == [e1, e2]              # 只 yield AgentEvent，ReplyStart 走回调
    assert started == [rs]
    assert tracer.on_event.call_count == 2
