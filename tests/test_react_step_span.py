"""P1-3 ReAct 每轮 span 单测。"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.orchestrator.react import ReActOrchestrator
from app.intent.models import Intent, IntentResult


def _orch():
    factory = MagicMock()
    orch = ReActOrchestrator(
        agent_factory=factory, think_client=MagicMock(),
        think_model_config={}, think_prompt="{{task}}{{scratch}}{{available_actions}}",
        max_steps=3,
    )
    return orch


@pytest.mark.asyncio
async def test_each_step_emits_span():
    orch = _orch()
    # 第 1 步 final，直接结束
    orch._think = AsyncMock(return_value={"is_final": True, "conclusion": "done", "thought": "t"})
    lf = MagicMock(); lf.enabled = True
    obs = MagicMock(); lf.start_observation.return_value = obs

    ir = IntentResult(rewritten_query="q",
                      intents=[Intent(id="i1", query="q", agent="a")],
                      relation="independent", execution_order=[])

    events = []

    async for ev in orch.run(ir, langfuse_service=lf):
        events.append(ev)

    lf.start_observation.assert_called()
    name = lf.start_observation.call_args_list[0].kwargs["name"]
    assert name == "react-step-1"
    out = lf.end_observation.call_args_list[0].kwargs["output"]
    assert out["isFinal"] is True
    assert "durationMs" in out


@pytest.mark.asyncio
async def test_no_langfuse_no_crash():
    orch = _orch()
    orch._think = AsyncMock(return_value={"is_final": True, "conclusion": "c", "thought": "t"})
    ir = IntentResult(rewritten_query="q",
                      intents=[Intent(id="i1", query="q", agent="a")],
                      relation="independent", execution_order=[])

    out = [ev async for ev in orch.run(ir, langfuse_service=None)]
    assert any("react_final" in e or "summary" in e for e in out)
