"""并行编排 LLM 汇总测试：多结果一律 LLM 汇总，失败/超时回退机械拼接。"""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

import app.orchestrator.parallel as parallel_module
from app.intent.models import Intent, IntentResult
from app.orchestrator.base import TaskResult
from app.orchestrator.parallel import ParallelOrchestrator

_QUERY = "查一下今天的新闻和北京天气"


def _make_orch(**kwargs) -> ParallelOrchestrator:
    return ParallelOrchestrator(agent_factory=MagicMock(), timeout=5.0, **kwargs)


def _make_ir() -> IntentResult:
    return IntentResult(
        rewritten_query=_QUERY,
        intents=[
            Intent(id="i1", query=_QUERY, agent="news_agent"),
            Intent(id="i2", query=_QUERY, agent="weather_agent"),
        ],
        relation="independent",
        execution_order=[],
    )


def _install_agents(orch, results_by_agent):
    """mock _run_single_agent：按 intent.agent yield 对应 TaskResult。"""
    async def _fake(intent, **kwargs):
        yield results_by_agent[intent.agent]
    orch._run_single_agent = _fake


def _parse_events(events):
    parsed = []
    for e in events:
        assert e.startswith("data: ")
        parsed.append(json.loads(e[len("data: "):].strip()))
    return parsed


def _summary_events(parsed):
    return [p for p in parsed if p["type"] == "summary"]


def _two_success_results():
    return {
        "news_agent": TaskResult(
            intent_id="i1", agent_id="news_agent", success=True, output="新闻结果A",
        ),
        "weather_agent": TaskResult(
            intent_id="i2", agent_id="weather_agent", success=True, output="天气结果B",
        ),
    }


@pytest.mark.asyncio
async def test_multi_result_llm_summary(monkeypatch):
    """多结果 + LLM 成功：summary 为 LLM 文本，prompt 含任务输出与用户问题。"""
    calls = {}

    async def fake_chat_complete(client, model_config, system_prompt,
                                 user_prompt, stage="llm-call"):
        calls["system"] = system_prompt
        calls["user"] = user_prompt
        calls["stage"] = stage
        return "这是汇总后的回答"

    monkeypatch.setattr(parallel_module, "chat_complete", fake_chat_complete)

    orch = _make_orch(
        summary_client=MagicMock(),
        summary_model_config={"model_name": "test-model"},
    )
    _install_agents(orch, _two_success_results())

    parsed = _parse_events([e async for e in orch.run(_make_ir())])
    types = [p["type"] for p in parsed]

    # 先 parallel_summary started，再 summary
    assert "parallel_summary" in types
    assert types.index("parallel_summary") < types.index("summary")

    summaries = _summary_events(parsed)
    assert len(summaries) == 1
    assert summaries[0]["content"] == "这是汇总后的回答"

    # prompt 含两个任务输出与 rewritten_query，埋点名称正确
    assert "新闻结果A" in calls["user"]
    assert "天气结果B" in calls["user"]
    assert _QUERY in calls["user"]
    assert calls["stage"] == "llm-parallel-summary"
    assert "汇总" in calls["system"]


@pytest.mark.asyncio
async def test_multi_result_llm_exception_fallback(monkeypatch):
    """多结果 + LLM 抛异常：回退机械拼接，无未捕获异常。"""
    async def fake_chat_complete(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(parallel_module, "chat_complete", fake_chat_complete)

    orch = _make_orch(
        summary_client=MagicMock(),
        summary_model_config={"model_name": "m"},
    )
    _install_agents(orch, _two_success_results())

    parsed = _parse_events([e async for e in orch.run(_make_ir())])
    summaries = _summary_events(parsed)
    assert len(summaries) == 1
    assert summaries[0]["content"].startswith("已为您完成 2 项任务")
    assert "新闻结果A" in summaries[0]["content"]
    assert "天气结果B" in summaries[0]["content"]


@pytest.mark.asyncio
async def test_multi_result_llm_timeout_fallback(monkeypatch):
    """多结果 + LLM 超时：回退机械拼接。"""
    async def fake_chat_complete(*a, **k):
        await asyncio.sleep(1)
        return "never"

    monkeypatch.setattr(parallel_module, "chat_complete", fake_chat_complete)

    orch = _make_orch(
        summary_client=MagicMock(),
        summary_model_config={"model_name": "m"},
        summary_timeout=0.05,
    )
    _install_agents(orch, _two_success_results())

    parsed = _parse_events([e async for e in orch.run(_make_ir())])
    summaries = _summary_events(parsed)
    assert len(summaries) == 1
    assert summaries[0]["content"].startswith("已为您完成 2 项任务")


@pytest.mark.asyncio
async def test_no_summary_client_direct_concat(monkeypatch):
    """未注入 summary_client（直接构造/测试场景）：不调 LLM，直接拼接。"""
    called = []

    async def fake_chat_complete(*a, **k):
        called.append(1)
        return "should not be used"

    monkeypatch.setattr(parallel_module, "chat_complete", fake_chat_complete)

    orch = _make_orch()  # 未注入 summary_client
    _install_agents(orch, _two_success_results())

    parsed = _parse_events([e async for e in orch.run(_make_ir())])
    assert not called
    summaries = _summary_events(parsed)
    assert len(summaries) == 1
    assert summaries[0]["content"].startswith("已为您完成 2 项任务")


@pytest.mark.asyncio
async def test_single_result_passthrough(monkeypatch):
    """单结果：不调 LLM，summary 直接透传该输出。"""
    called = []

    async def fake_chat_complete(*a, **k):
        called.append(1)
        return "should not be used"

    monkeypatch.setattr(parallel_module, "chat_complete", fake_chat_complete)

    orch = _make_orch(
        summary_client=MagicMock(),
        summary_model_config={"model_name": "m"},
    )
    _install_agents(orch, {
        "news_agent": TaskResult(
            intent_id="i1", agent_id="news_agent", success=True, output="唯一结果",
        ),
    })
    ir = IntentResult(
        rewritten_query=_QUERY,
        intents=[Intent(id="i1", query=_QUERY, agent="news_agent")],
        relation="independent",
        execution_order=[],
    )

    parsed = _parse_events([e async for e in orch.run(ir)])
    assert not called
    assert not any(p["type"] == "parallel_summary" for p in parsed)
    summaries = _summary_events(parsed)
    assert len(summaries) == 1
    assert summaries[0]["content"] == "唯一结果"


@pytest.mark.asyncio
async def test_failed_task_marked_in_prompt(monkeypatch):
    """失败任务进入 prompt 时带 - 失败 标记（成功为 - 成功）。"""
    calls = {}

    async def fake_chat_complete(client, model_config, system_prompt,
                                 user_prompt, stage="llm-call"):
        calls["user"] = user_prompt
        return "ok"

    monkeypatch.setattr(parallel_module, "chat_complete", fake_chat_complete)

    orch = _make_orch(
        summary_client=MagicMock(),
        summary_model_config={"model_name": "m"},
    )
    _install_agents(orch, {
        "news_agent": TaskResult(
            intent_id="i1", agent_id="news_agent", success=True, output="新闻结果A",
        ),
        "weather_agent": TaskResult(
            intent_id="i2", agent_id="weather_agent",
            success=False, output="执行超时",
        ),
    })

    parsed = _parse_events([e async for e in orch.run(_make_ir())])
    assert _summary_events(parsed)
    assert "- 成功" in calls["user"]
    assert "- 失败" in calls["user"]
    assert "执行超时" in calls["user"]


@pytest.mark.asyncio
async def test_long_output_truncated_in_prompt(monkeypatch):
    """超长任务输出进入 prompt 时截断到上限并追加标记。"""
    calls = {}

    async def fake_chat_complete(client, model_config, system_prompt,
                                 user_prompt, stage="llm-call"):
        calls["user"] = user_prompt
        return "ok"

    monkeypatch.setattr(parallel_module, "chat_complete", fake_chat_complete)

    orch = _make_orch(
        summary_client=MagicMock(),
        summary_model_config={"model_name": "m"},
    )
    _install_agents(orch, {
        "news_agent": TaskResult(
            intent_id="i1", agent_id="news_agent",
            success=True, output="x" * 5000,
        ),
        "weather_agent": TaskResult(
            intent_id="i2", agent_id="weather_agent", success=True, output="天气结果B",
        ),
    })

    parsed = _parse_events([e async for e in orch.run(_make_ir())])
    assert _summary_events(parsed)
    assert "内容过长已截断" in calls["user"]
    # 截断后连续 x 不超过 4000
    assert "x" * 4001 not in calls["user"]
