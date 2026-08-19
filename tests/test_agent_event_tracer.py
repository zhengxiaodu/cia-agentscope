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
    def __init__(self, tcid, state, metadata=None):
        self.tool_call_id = tcid; self.state = state
        self.metadata = metadata if metadata is not None else {}


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


# ---- citations 累积（从 ToolResultEndEvent.metadata 提取）----


def test_citations_accumulated_from_metadata(monkeypatch):
    """带 citations 的 ToolResultEndEvent 应被累积。"""
    import app.utils.agent_event_tracer as m
    monkeypatch.setattr(m, "ToolResultEndEvent", FakeToolEnd)
    t, _ = _tracer()
    cites = [{"position": 1, "document_name": "docA", "content": "..."}]
    t.on_event(FakeToolEnd("c1", "ok", metadata={"citations": cites}))
    assert t.consume_citations() == cites


def test_citations_accumulated_even_when_langfuse_disabled(monkeypatch):
    """langfuse 未启用时 citations 仍应累积（独立于埋点逻辑）。"""
    import app.utils.agent_event_tracer as m
    monkeypatch.setattr(m, "ToolResultEndEvent", FakeToolEnd)
    lf = MagicMock(); lf.enabled = False
    t = AgentEventTracer(lf, "agent-x")
    cites = [{"position": 1, "document_name": "docB"}]
    t.on_event(FakeToolEnd("c1", "ok", metadata={"citations": cites}))
    assert t.consume_citations() == cites


def test_citations_not_collected_without_metadata(monkeypatch):
    """无 metadata 或无 citations 字段的事件不影响累积。"""
    import app.utils.agent_event_tracer as m
    monkeypatch.setattr(m, "ToolResultEndEvent", FakeToolEnd)
    t, _ = _tracer()
    t.on_event(FakeToolEnd("c1", "ok"))  # 默认空 metadata
    t.on_event(FakeToolEnd("c2", "ok", metadata={"other": "x"}))  # 无 citations
    assert t.consume_citations() == []


def test_consume_citations_clears_after_read(monkeypatch):
    """consume_citations 二次调用返回空。"""
    import app.utils.agent_event_tracer as m
    monkeypatch.setattr(m, "ToolResultEndEvent", FakeToolEnd)
    t, _ = _tracer()
    t.on_event(FakeToolEnd("c1", "ok", metadata={"citations": [{"a": 1}]}))
    assert t.consume_citations() == [{"a": 1}]
    assert t.consume_citations() == []


def test_citations_aggregated_across_multiple_tool_calls(monkeypatch):
    """多次工具调用的 citations 应聚合（extend）。"""
    import app.utils.agent_event_tracer as m
    monkeypatch.setattr(m, "ToolResultEndEvent", FakeToolEnd)
    t, _ = _tracer()
    t.on_event(FakeToolEnd("c1", "ok", metadata={"citations": [{"i": 1}]}))
    t.on_event(FakeToolEnd("c2", "ok", metadata={"citations": [{"i": 2}]}))
    assert t.consume_citations() == [{"i": 1}, {"i": 2}]
