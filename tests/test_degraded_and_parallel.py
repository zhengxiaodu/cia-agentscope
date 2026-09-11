"""P1-4 降级入 trace + parallel 异常保真。"""
from app.orchestrator.base import TaskResult


def test_taskresult_has_metadata_field():
    r = TaskResult(intent_id="i", agent_id="a", metadata={"errorClass": "ValueError"})
    assert r.metadata == {"errorClass": "ValueError"}


def test_taskresult_metadata_defaults_none():
    r = TaskResult(intent_id="i", agent_id="a")
    assert r.metadata is None


import asyncio
import pytest
from unittest.mock import MagicMock

from app.orchestrator.parallel import ParallelOrchestrator
from app.intent.models import Intent, IntentResult


@pytest.mark.asyncio
async def test_parallel_runner_preserves_exception():
    orch = ParallelOrchestrator(agent_factory=MagicMock(), timeout=5.0)

    async def _boom(*a, **k):
        raise ValueError("branch boom")
        yield  # make it an async gen
    orch._run_single_agent = _boom

    ir = IntentResult(rewritten_query="q",
                      intents=[Intent(id="i1", query="q", agent="a")],
                      relation="independent", execution_order=[])

    async for ev in orch.run(ir):
        pass
    # runner 把异常写入 _last_results 的 metadata
    failed = [r for r in orch._last_results if not r.success]
    assert failed and failed[0].metadata is not None
    assert "ValueError" in failed[0].metadata["errorClass"]

