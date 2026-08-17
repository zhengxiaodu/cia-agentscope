"""P1-6 pipeline 编排级 span 单测。"""
import pytest
from unittest.mock import MagicMock

from app.orchestrator.pipeline import PipelineOrchestrator
from app.orchestrator.base import TaskResult
from app.intent.models import Intent, IntentResult


def _ir(n):
    return IntentResult(
        rewritten_query="q",
        intents=[Intent(id=f"i{k}", query="q", agent="a") for k in range(n)],
        relation="independent", execution_order=[],
    )


@pytest.mark.asyncio
async def test_pipeline_span_records_total_and_stopped():
    orch = PipelineOrchestrator(agent_factory=MagicMock(), step_timeout=5.0)

    # 第 1 步失败 → 流水线在第 1 步终止
    async def _fail_first(intent, **k):
        yield TaskResult(intent_id=intent.id, agent_id="a", success=False, output="boom")
    orch._run_single_agent = _fail_first

    lf = MagicMock(); lf.enabled = True
    obs = MagicMock(); lf.start_observation.return_value = obs

    async for _ in orch.run(_ir(3), langfuse_service=lf):
        pass

    lf.start_observation.assert_called_once()
    assert lf.start_observation.call_args.kwargs["name"] == "orchestrate-pipeline"
    out = lf.end_observation.call_args.kwargs["output"]
    assert out["totalSteps"] == 3
    assert out["stoppedAtStep"] == 1
    assert out["success"] is False
