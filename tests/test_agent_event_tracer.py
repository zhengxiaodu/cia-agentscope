"""P1-2 AgentEventTracer 单测：reply_id 配对、悬挂收尾、汇总、异常隔离。"""
from unittest.mock import MagicMock

from app.utils.agent_event_tracer import AgentEventTracer


class FakeModelStart:
    def __init__(self, reply_id, model_name):
        self.reply_id = reply_id; self.model_name = model_name


class FakeModelEnd:
    def __init__(self, reply_id, it, ot, reason="stop"):
        self.reply_id = reply_id; self.input_tokens = it
        self.output_tokens = ot; self.finished_reason = reason


class FakeToolStart:
    def __init__(self, tcid, name):
        self.tool_call_id = tcid; self.tool_call_name = name


class FakeToolEnd:
    def __init__(self, tcid, state):
        self.tool_call_id = tcid; self.state = state


def _tracer():
    lf = MagicMock(); lf.enabled = True
    lf.start_observation.return_value = MagicMock()
    t = AgentEventTracer(lf, "agent-x")
    return t, lf


def test_model_call_pairs_and_usage_summed(monkeypatch):
    import app.utils.agent_event_tracer as m
    monkeypatch.setattr(m, "ModelCallStartEvent", FakeModelStart)
    monkeypatch.setattr(m, "ModelCallEndEvent", FakeModelEnd)
    t, lf = _tracer()
    t.on_event(FakeModelStart("r1", "deepseek-v4"))
    t.on_event(FakeModelEnd("r1", 100, 40))
    assert t.summary()["llmCalls"] == 1
    assert t.summary()["inputTokens"] == 100
    assert t.summary()["outputTokens"] == 40


def test_reply_id_reuse_closes_dangling(monkeypatch):
    """同一 reply_id 再次 START 时，先结束悬挂项，不泄漏。"""
    import app.utils.agent_event_tracer as m
    monkeypatch.setattr(m, "ModelCallStartEvent", FakeModelStart)
    monkeypatch.setattr(m, "ModelCallEndEvent", FakeModelEnd)
    t, lf = _tracer()
    t.on_event(FakeModelStart("r1", "m"))
    t.on_event(FakeModelStart("r1", "m"))   # 复用，触发悬挂收尾
    t.on_event(FakeModelEnd("r1", 1, 1))
    assert t.summary()["llmCalls"] == 2
    assert lf.end_observation.call_count >= 2


def test_tool_failure_counted(monkeypatch):
    import app.utils.agent_event_tracer as m
    monkeypatch.setattr(m, "ToolCallStartEvent", FakeToolStart)
    monkeypatch.setattr(m, "ToolResultEndEvent", FakeToolEnd)
    t, lf = _tracer()
    t.on_event(FakeToolStart("c1", "Bash"))
    t.on_event(FakeToolEnd("c1", "error"))
    assert t.summary()["toolCalls"] == 1
    assert t.summary()["toolFailures"] == 1


def test_close_ends_hanging(monkeypatch):
    import app.utils.agent_event_tracer as m
    monkeypatch.setattr(m, "ModelCallStartEvent", FakeModelStart)
    t, lf = _tracer()
    t.on_event(FakeModelStart("r1", "m"))   # 不 END
    t.close()
    lf.end_observation.assert_called()      # close 收尾悬挂 generation


def test_on_event_swallows_errors():
    """tracer 内部异常不得冒泡影响业务流。"""
    lf = MagicMock(); lf.enabled = True
    lf.start_observation.side_effect = RuntimeError("langfuse down")
    t = AgentEventTracer(lf, "agent-x")

    class _Unknown:  # 不匹配任何 isinstance 分支
        pass
    t.on_event(_Unknown())   # 不抛异常即通过


def test_disabled_is_noop():
    lf = MagicMock(); lf.enabled = False
    t = AgentEventTracer(lf, "agent-x")
    t.on_event(object())
    t.close()
    lf.start_observation.assert_not_called()
