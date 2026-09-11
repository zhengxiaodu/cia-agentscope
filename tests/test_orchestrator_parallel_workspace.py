"""工作区准备与意图链路并行执行的时序、失败语义与 task 清理（纯 mock）。

不起真实沙箱与 LLM：把 _load_config_bundle / _build_intent_components /
_prepare_workspace_components 全部替换为可控桩，只验证 run() 的编排逻辑。
"""
import asyncio
import json

import pytest

from app.services.orchestrator_service import OrchestratorService
from app.intent.models import Intent

MESSAGES = [{"role": "user", "content": "帮我查一下今天的新闻"}]


def _events(chunks: list[str]) -> list[dict]:
    """把 SSE 字符串解析成 dict 列表，便于按 type 断言。"""
    out = []
    for chunk in chunks:
        payload = chunk[len("data: "):].strip()
        out.append(json.loads(payload))
    return out


# 意图链路的桩必须真的耗时，否则"并行"与"串行"在总耗时上无法区分，
# 时序断言就成了永真断言。两段各 0.2s，合计 0.4s。
_INTENT_STEP_DELAY = 0.2


class _FakeRewriter:
    def __init__(self, marks: list):
        self._marks = marks

    async def rewrite(self, user_input, history):
        self._marks.append(("rewrite_start", asyncio.get_running_loop().time()))
        await asyncio.sleep(_INTENT_STEP_DELAY)
        return user_input, {}


class _FakeRecognizer:
    def __init__(self, marks: list):
        self._marks = marks

    async def recognize_intents(self, query, history):
        self._marks.append(("recognize", asyncio.get_running_loop().time()))
        await asyncio.sleep(_INTENT_STEP_DELAY)
        return [Intent(id="general_chat", query=query, agent="general_agent")], {}

    async def plan_orchestration(self, query, intents):
        return "independent", [], {}

    def get_orchestration_mode(self, intent_result):
        return "parallel"


class _FakeOrchestrator:
    def __init__(self):
        self._last_results = []

    async def run(self, intent_result, session_id=None, agent_states=None, langfuse_service=None):
        yield 'data: {"type": "fake_agent_done"}\n\n'


class _FakeRegistry:
    def get_definition(self, agent_id):
        return object()


def _make_service(marks: list, ws_delay: float = 0.3, ws_error: Exception | None = None):
    """构造只保留 run() 编排逻辑的服务实例，三个装配方法全部打桩。"""
    svc = OrchestratorService.__new__(OrchestratorService)
    svc._last_orchestrator = None
    svc._last_agent_ids = []
    svc._last_success = True
    svc._orchestrator_params = {
        "parallel_timeout": 60, "pipeline_step_timeout": 60, "react_max_steps": 8,
    }

    async def _fake_bundle(user_id, redis_client):
        return {
            "merged_intents": [], "merged_agents": [],
            "merged_skills": [], "default_orchestration": {},
        }

    def _fake_intent_components(fused):
        return _FakeRewriter(marks), _FakeRecognizer(marks)

    async def _fake_workspace(**kwargs):
        await asyncio.sleep(ws_delay)
        marks.append(("workspace_done", asyncio.get_running_loop().time()))
        if ws_error is not None:
            raise ws_error
        return _FakeRegistry(), object()

    svc._load_config_bundle = _fake_bundle
    svc._build_intent_components = _fake_intent_components
    svc._prepare_workspace_components = _fake_workspace
    svc._create_orchestrator = lambda mode, agent_factory: _FakeOrchestrator()
    return svc


@pytest.mark.asyncio
async def test_rewrite_starts_before_workspace_ready():
    """改写必须在工作区准备完成之前就开始——这是并行生效的判据。"""
    marks: list = []
    svc = _make_service(marks, ws_delay=0.3)

    chunks = [c async for c in svc.run(MESSAGES)]

    names = [m[0] for m in marks]
    assert names.index("rewrite_start") < names.index("workspace_done")
    assert any(e["type"] == "fake_agent_done" for e in _events(chunks))


@pytest.mark.asyncio
async def test_total_time_less_than_serial_sum():
    """总耗时应接近 max(工作区, 意图链路) 而非两者之和。

    工作区 0.3s、意图链路 0.4s：串行下限 0.7s，并行上限约 0.4s。
    阈值取 0.6s——高于并行值、低于串行值，改动前必失败、改动后必通过。
    """
    marks: list = []
    svc = _make_service(marks, ws_delay=0.3)
    loop = asyncio.get_running_loop()

    t0 = loop.time()
    [c async for c in svc.run(MESSAGES)]
    elapsed = loop.time() - t0

    assert elapsed < 0.6


@pytest.mark.asyncio
async def test_workspace_failure_yields_error_after_intent_events():
    """工作区失败 → 意图事件先出、error 事件后出，且不向上抛异常。"""
    marks: list = []
    svc = _make_service(marks, ws_delay=0.05, ws_error=RuntimeError("sandbox unreachable"))

    events = _events([c async for c in svc.run(MESSAGES)])

    types = [e["type"] for e in events]
    assert "query_rewritten" in types
    assert "intents_recognized" in types
    assert types[-1] == "error"
    assert events[-1]["message"] == "环境准备失败: sandbox unreachable"
    assert types.index("query_rewritten") < types.index("error")
    assert svc._last_success is False


@pytest.mark.asyncio
async def test_generator_closed_early_cancels_workspace_task():
    """客户端断连（生成器提前关闭）→ 工作区 task 被取消，不留悬挂 task。"""
    marks: list = []
    svc = _make_service(marks, ws_delay=5.0)

    gen = svc.run(MESSAGES)
    first = await gen.__anext__()
    await gen.aclose()
    await asyncio.sleep(0)

    assert first.startswith("data: ")
    pending = [
        t for t in asyncio.all_tasks()
        if t.get_name() == "workspace-prepare" and not t.done()
    ]
    assert pending == []
    assert ("workspace_done", ) not in [(m[0],) for m in marks]


@pytest.mark.asyncio
async def test_agent_id_path_awaits_workspace_immediately():
    """agent_id 直连分支必须先拿到 registry 再走单 agent 路径。"""
    marks: list = []
    svc = _make_service(marks, ws_delay=0.05)

    consumed: list = []

    async def _fake_single(*args, **kwargs):
        consumed.append(args[0])
        yield 'data: {"type": "direct_done"}\n\n'

    svc._run_single_agent_path = _fake_single

    events = _events([c async for c in svc.run(MESSAGES, agent_id="general_agent")])

    assert [e["type"] for e in events] == ["direct_done"]
    assert isinstance(consumed[0], _FakeRegistry)


@pytest.mark.asyncio
async def test_workspace_failure_on_agent_id_path():
    """agent_id 分支的工作区失败同样产出 error 事件、不抛。"""
    marks: list = []
    svc = _make_service(marks, ws_delay=0.01, ws_error=RuntimeError("boom"))

    events = _events([c async for c in svc.run(MESSAGES, agent_id="general_agent")])

    assert events[-1]["type"] == "error"
    assert events[-1]["message"] == "环境准备失败: boom"
