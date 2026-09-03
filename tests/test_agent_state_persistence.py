"""会话记忆加载修复：枚举序列化根因 + 历史脏数据迁移。

覆盖：
1. model_dump(mode="json") 保存的 state 可被完整加载
2. 历史脏数据 "PermissionMode.BYPASS" 被迁移为 "bypass" 后可正常加载
3. _load_agent_state 各异常分支返回 None
"""
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from agentscope.state import AgentState
from agentscope.permission import PermissionContext, PermissionMode

from app.services.orchestrator_service import OrchestratorService


def _make_service() -> OrchestratorService:
    """构造一个最小可用的 OrchestratorService 实例（绕过 create 工厂）。"""
    return OrchestratorService(
        model_config={},
        prompts={},
        orchestrator_params={},
        intent_client=MagicMock(),
        intent_model_cfg={},
        think_prompt="",
        workspace_manager=MagicMock(),
    )


# ================================================================
# _migrate_legacy_enum_values
# ================================================================

def test_migrate_fixes_permission_mode_bypass():
    """历史脏数据 "PermissionMode.BYPASS" → "bypass"。"""
    state_dict = {"permission_context": {"mode": "PermissionMode.BYPASS"}}
    fixed = OrchestratorService._migrate_legacy_enum_values(state_dict)
    assert fixed["permission_context"]["mode"] == "bypass"


def test_migrate_fixes_permission_mode_default():
    """历史脏数据 "PermissionMode.DEFAULT" → "default"。"""
    state_dict = {"permission_context": {"mode": "PermissionMode.DEFAULT"}}
    fixed = OrchestratorService._migrate_legacy_enum_values(state_dict)
    assert fixed["permission_context"]["mode"] == "default"


def test_migrate_preserves_valid_mode():
    """合法的 "bypass" 不受影响。"""
    state_dict = {"permission_context": {"mode": "bypass"}}
    fixed = OrchestratorService._migrate_legacy_enum_values(state_dict)
    assert fixed["permission_context"]["mode"] == "bypass"


def test_migrate_handles_missing_permission_context():
    """无 permission_context 字段时不报错。"""
    state_dict = {"context": []}
    fixed = OrchestratorService._migrate_legacy_enum_values(state_dict)
    assert fixed == {"context": []}


def test_migrate_handles_non_dict():
    """非 dict 输入原样返回。"""
    assert OrchestratorService._migrate_legacy_enum_values(None) is None
    assert OrchestratorService._migrate_legacy_enum_values("str") == "str"


# ================================================================
# _load_agent_state
# ================================================================

@pytest.mark.asyncio
async def test_load_returns_none_when_no_session_service():
    svc = _make_service()
    assert await svc._load_agent_state(None, "s1", "a1") is None


@pytest.mark.asyncio
async def test_load_returns_none_when_no_session_id():
    svc = _make_service()
    assert await svc._load_agent_state(MagicMock(), None, "a1") is None


@pytest.mark.asyncio
async def test_load_returns_none_when_dao_returns_none():
    svc = _make_service()
    session_service = MagicMock()
    session_service.load_agent_state = AsyncMock(return_value=None)
    assert await svc._load_agent_state(session_service, "s1", "a1") is None


@pytest.mark.asyncio
async def test_load_returns_none_when_dao_returns_empty():
    svc = _make_service()
    session_service = MagicMock()
    session_service.load_agent_state = AsyncMock(return_value={})
    assert await svc._load_agent_state(session_service, "s1", "a1") is None


@pytest.mark.asyncio
async def test_load_returns_none_when_dao_raises():
    svc = _make_service()
    session_service = MagicMock()
    session_service.load_agent_state = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    assert await svc._load_agent_state(session_service, "s1", "a1") is None


@pytest.mark.asyncio
async def test_load_full_validate_success():
    """model_dump(mode="json") 保存的 state 可被完整加载。"""
    original = AgentState(
        session_id="s1",
        permission_context=PermissionContext(mode=PermissionMode.BYPASS),
    )
    state_dict = original.model_dump(mode="json")

    svc = _make_service()
    session_service = MagicMock()
    session_service.load_agent_state = AsyncMock(return_value=state_dict)

    loaded = await svc._load_agent_state(session_service, "s1", "a1")
    assert loaded is not None
    assert isinstance(loaded, AgentState)
    assert loaded.permission_context.mode == PermissionMode.BYPASS


@pytest.mark.asyncio
async def test_load_migrates_legacy_permission_mode():
    """历史脏数据 permission_context.mode="PermissionMode.BYPASS" 迁移后可加载。"""
    state_dict = {
        "session_id": "s1",
        "permission_context": {"mode": "PermissionMode.BYPASS"},
        "context": [],
    }

    svc = _make_service()
    session_service = MagicMock()
    session_service.load_agent_state = AsyncMock(return_value=state_dict)

    loaded = await svc._load_agent_state(session_service, "s1", "a1")
    assert loaded is not None
    assert loaded.permission_context.mode == PermissionMode.BYPASS


@pytest.mark.asyncio
async def test_load_logs_warning_on_validate_failure(caplog):
    """反序列化失败时记录 warning 日志（含异常栈）。"""
    state_dict = {
        "permission_context": {"mode": "TOTALLY_INVALID_VALUE"},
        "context": [],
    }

    svc = _make_service()
    session_service = MagicMock()
    session_service.load_agent_state = AsyncMock(return_value=state_dict)

    with caplog.at_level(logging.WARNING, logger="app.services.orchestrator_service"):
        result = await svc._load_agent_state(session_service, "s1", "a1")

    assert result is None
    assert any("反序列化" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_load_logs_warning_on_dao_failure(caplog):
    """DAO 读取异常时记录 warning 日志。"""
    svc = _make_service()
    session_service = MagicMock()
    session_service.load_agent_state = AsyncMock(
        side_effect=RuntimeError("connection lost")
    )

    with caplog.at_level(logging.WARNING, logger="app.services.orchestrator_service"):
        result = await svc._load_agent_state(session_service, "s1", "a1")

    assert result is None
    assert any("读取" in r.message and "失败" in r.message for r in caplog.records)


# ================================================================
# 保存路径：model_dump(mode="json") 确保枚举序列化为值
# ================================================================

def test_model_dump_json_mode_serializes_enum_as_value():
    """验证 AgentState.model_dump(mode="json") 把 PermissionMode.BYPASS 序列化为 "bypass"。"""
    state = AgentState(
        session_id="s1",
        permission_context=PermissionContext(mode=PermissionMode.BYPASS),
    )
    dumped = state.model_dump(mode="json")
    assert dumped["permission_context"]["mode"] == "bypass"

    # 进一步验证 json.dumps 后可被 json.loads + model_validate 还原
    raw = json.dumps(dumped, ensure_ascii=False)
    restored = AgentState.model_validate(json.loads(raw))
    assert restored.permission_context.mode == PermissionMode.BYPASS


def test_model_dump_default_mode_keeps_enum_instance():
    """验证 model_dump()（无 mode 参数）保留枚举实例 → json.dumps(default=str) 会产生脏数据。

    此测试作为根因回归保护：如果未来有人误改回 model_dump()，
    此测试会提醒其枚举不会自动变成 "bypass"。
    """
    state = AgentState(
        session_id="s1",
        permission_context=PermissionContext(mode=PermissionMode.BYPASS),
    )
    dumped = state.model_dump()
    # 默认模式下 mode 仍是枚举实例（或其 value，取决于 pydantic 版本），
    # 但 json.dumps(default=str) 会把它变成 "PermissionMode.BYPASS"
    raw = json.dumps(dumped, default=str)
    parsed = json.loads(raw)
    # 这就是历史脏数据的来源
    assert parsed["permission_context"]["mode"] == "PermissionMode.BYPASS"
