"""stage_status 阶段事件测试：事件构造 + 工作区获取/创建阶段序列。"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.orchestrator_service import OrchestratorService
from app.utils.sse_events import Stage, stage_status_event


def _make_service() -> OrchestratorService:
    """构造最小可用的 OrchestratorService（绕过 create 工厂）。"""
    return OrchestratorService(
        model_config={},
        prompts={},
        orchestrator_params={},
        intent_client=MagicMock(),
        intent_model_cfg={},
        think_prompt="",
        workspace_manager=MagicMock(),
    )


# ---- 事件构造 ----

def test_stage_status_event_format():
    """stage_status_event 输出标准 SSE 格式与字段。"""
    ev = stage_status_event(Stage.WORKSPACE_CREATE, "started", "正在创建工作区...")
    assert ev.startswith("data: ")
    assert ev.endswith("\n\n")
    payload = json.loads(ev[6:].strip())
    assert payload == {
        "type": "stage_status",
        "stage": "workspace_create",
        "status": "started",
        "message": "正在创建工作区...",
    }


def test_stage_status_event_default_message():
    """message 缺省为空字符串。"""
    payload = json.loads(stage_status_event(Stage.WORKSPACE_GET, "done")[6:].strip())
    assert payload["message"] == ""


# ---- 工作区获取/创建阶段 ----

@pytest.mark.asyncio
async def test_iter_workspace_stage_get_hit():
    """命中已有工作区：get started → get done，不触发创建。"""
    svc = _make_service()
    workspace = MagicMock(workspace_id="ws-1")
    svc._workspace_manager.get_workspace = AsyncMock(return_value=workspace)
    svc._workspace_manager.create_workspace = AsyncMock()

    holder = {}
    events = []
    async for ev in svc._iter_workspace_stage(
        "u1", "sess-1", ["/skills/a"], None, holder,
    ):
        events.append(ev)

    parsed = [json.loads(e[6:].strip()) for e in events]
    assert [(p["stage"], p["status"]) for p in parsed] == [
        ("workspace_get", "started"),
        ("workspace_get", "done"),
    ]
    assert all(p["type"] == "stage_status" for p in parsed)
    assert holder["workspace"] is workspace
    svc._workspace_manager.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_iter_workspace_stage_create_on_miss():
    """未命中（首次/过期/崩溃）：get started → create started → create done。"""
    svc = _make_service()
    created = MagicMock(workspace_id="ws-new")
    svc._workspace_manager.get_workspace = AsyncMock(return_value=None)
    svc._workspace_manager.create_workspace = AsyncMock(return_value=created)

    holder = {}
    events = []
    async for ev in svc._iter_workspace_stage(
        "u1", "sess-1", ["/skills/a", "/skills/b"], None, holder,
    ):
        events.append(ev)

    parsed = [json.loads(e[6:].strip()) for e in events]
    assert [(p["stage"], p["status"]) for p in parsed] == [
        ("workspace_get", "started"),
        ("workspace_create", "started"),
        ("workspace_create", "done"),
    ]
    assert holder["workspace"] is created
    svc._workspace_manager.create_workspace.assert_awaited_once_with(
        user_id="u1",
        session_id="sess-1",
        skill_dirs=["/skills/a", "/skills/b"],
        langfuse_service=None,
    )


@pytest.mark.asyncio
async def test_iter_workspace_stage_langfuse_span():
    """提供 langfuse_service 时记录 workspace-load 子 span（含 workspace_id 输出）。"""
    from contextlib import contextmanager

    svc = _make_service()
    workspace = MagicMock(workspace_id="ws-1")
    workspace_id = "ws-1"
    svc._workspace_manager.get_workspace = AsyncMock(return_value=workspace)

    span_updates = []

    @contextmanager
    def fake_start_span(name, input=None):
        span = MagicMock()
        span.update = lambda **kw: span_updates.append({"name": name, **kw})
        yield span

    lf = MagicMock()
    lf.start_span = fake_start_span

    holder = {}
    async for ev in svc._iter_workspace_stage(
        "u1", "sess-1", [], lf, holder,
    ):
        pass

    assert len(span_updates) == 1
    assert span_updates[0]["name"] == "workspace-load"
    assert span_updates[0]["output"]["workspace_id"] == "ws-1"
