"""_build_request_components 拆分后的三个方法各自的行为（纯 mock）。

只验证拆分不改语义：配置读取的命中/兜底、意图组件的装配、
工作区组件对 get_workspace → create_workspace 的回退顺序。
"""
import json
import os

import pytest

from agentscope.skill import Skill

from app.services.orchestrator_service import OrchestratorService

FUSED = {
    "merged_intents": [
        {"id": "general_chat", "name": "闲聊", "description": "闲聊", "agent": "general_agent"},
    ],
    "merged_agents": [
        {"id": "general_agent", "name": "通用助手", "skills": [], "system_prompt": "你是助手"},
    ],
    "merged_skills": [
        {"name": "bocha_search", "directory": "/workspace/skills/bocha_search"},
    ],
    "default_orchestration": {"relation": "independent"},
}

# 仓库内真实 config 的绝对路径（app.config 的相对路径以 app/ 为 CWD，
# 测试从仓库根跑时需指向绝对路径）
_REPO_CONFIG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")


class _FakeRedis:
    def __init__(self, payload: dict | None):
        self._payload = payload

    async def get(self, key: str):
        if self._payload is None:
            return None
        return json.dumps(self._payload).encode("utf-8")


class _FakeSandbox:
    def __init__(self):
        self.id = "sbx-1"
        self.workdir = "/data/workspaces/s1"
        self.workspace_id = "user-u1"


class _FakeWorkspaceManager:
    """记录调用顺序；get_workspace 返回 None 时应回退到 create_workspace。"""

    def __init__(self, existing=None):
        self.calls: list[str] = []
        self._existing = existing
        self.created_skill_dirs = None

    async def get_workspace(self, user_id, session_id):
        self.calls.append("get")
        return self._existing

    async def create_workspace(self, user_id, session_id, skill_dirs=None, langfuse_service=None):
        self.calls.append("create")
        self.created_skill_dirs = skill_dirs
        return _FakeSandbox()

    async def list_skills(self, user_id="", session_id=""):
        self.calls.append("list_skills")
        # 生产契约：返回 agentscope Skill 对象（Toolkit 不接受 dict）
        return [
            Skill(name="bocha_search", description="搜索",
                  dir="/workspace/skills/bocha_search", markdown="", updated_at=0.0),
            Skill(name="mineru", description="解析",
                  dir="/workspace/skills/mineru", markdown="", updated_at=0.0),
        ]


def _make_service(workspace_manager=None) -> OrchestratorService:
    """只填三个新方法用到的属性。"""
    svc = OrchestratorService.__new__(OrchestratorService)
    svc._prompts = {
        "rewrite": "改写提示词",
        "intent_recognition": "识别提示词",
        "intent_orchestration": "编排提示词",
    }
    svc._intent_client = object()
    svc._intent_model_cfg = {"model": "qwen-plus"}
    svc._workspace_manager = workspace_manager
    svc._create_model_fn = None
    svc._last_success = True
    return svc


def _patch_config_paths(monkeypatch):
    """把相对 config 路径指到仓库内绝对路径，供 base-only 兜底读取。"""
    monkeypatch.setattr(
        "app.services.orchestrator_service.AGENT_CONFIG_PATH",
        os.path.join(_REPO_CONFIG, "agent_config.yml"),
    )
    monkeypatch.setattr(
        "app.services.orchestrator_service.INTENT_CONFIG_PATH",
        os.path.join(_REPO_CONFIG, "intent_config.yml"),
    )
    monkeypatch.setattr(
        "app.services.orchestrator_service.SKILL_CONFIG_PATH",
        os.path.join(_REPO_CONFIG, "skill_config.yml"),
    )


@pytest.mark.asyncio
async def test_load_config_bundle_uses_cache_when_present():
    """Redis 命中 → 直接返回缓存内容，四个键齐全。"""
    svc = _make_service()

    bundle = await svc._load_config_bundle("u1", _FakeRedis(FUSED))

    assert bundle == FUSED


@pytest.mark.asyncio
async def test_load_config_bundle_falls_back_to_base_only_on_miss(monkeypatch):
    """Redis 未命中 → 走 base-only 融合（不请求 mng），键仍齐全。"""
    _patch_config_paths(monkeypatch)
    svc = _make_service()

    bundle = await svc._load_config_bundle("u1", _FakeRedis(None))

    assert set(bundle) == {
        "merged_intents", "merged_agents", "merged_skills", "default_orchestration",
    }
    # base-only 兜底读的是仓库内 YAML，技能配置非空
    assert bundle["merged_skills"]


@pytest.mark.asyncio
async def test_load_config_bundle_without_redis_returns_base_only(monkeypatch):
    """redis_client 为 None 时同样兜底，不抛异常。"""
    _patch_config_paths(monkeypatch)
    svc = _make_service()

    bundle = await svc._load_config_bundle("u1", None)

    assert bundle["merged_agents"]


def test_build_intent_components_wires_prompts():
    """改写器与识别器拿到对应的 prompt 与 default_orchestration。"""
    svc = _make_service()

    rewriter, recognizer = svc._build_intent_components(FUSED)

    assert rewriter._rewrite_prompt == "改写提示词"
    assert recognizer._recognition_prompt == "识别提示词"
    assert recognizer._orchestration_prompt == "编排提示词"
    assert recognizer._default_orchestration == {"relation": "independent"}


@pytest.mark.asyncio
async def test_prepare_workspace_components_creates_when_absent(monkeypatch):
    """get_workspace 返回 None → 回退 create_workspace，并透传技能目录。"""
    monkeypatch.setattr(
        "app.services.orchestrator_service.WORKSPACE_BACKEND", "opensandbox"
    )
    mgr = _FakeWorkspaceManager(existing=None)
    svc = _make_service(mgr)

    registry, factory = await svc._prepare_workspace_components(
        fused=FUSED,
        user_id="u1",
        redis_client=None,
        user_id_safe="u1",
        session_id_safe="s1",
        search_enabled=True,
        skills=[],
        langfuse_service=None,
    )

    assert mgr.calls == ["get", "create", "list_skills"]
    assert mgr.created_skill_dirs == ["/workspace/skills/bocha_search"]
    assert registry.get_definition("general_agent") is not None
    assert factory is not None


@pytest.mark.asyncio
async def test_prepare_workspace_components_reuses_existing(monkeypatch):
    """get_workspace 命中 → 不调 create_workspace。"""
    monkeypatch.setattr(
        "app.services.orchestrator_service.WORKSPACE_BACKEND", "opensandbox"
    )
    mgr = _FakeWorkspaceManager(existing=_FakeSandbox())
    svc = _make_service(mgr)

    await svc._prepare_workspace_components(
        fused=FUSED, user_id="u1", redis_client=None,
        user_id_safe="u1", session_id_safe="s1",
        search_enabled=True, skills=[], langfuse_service=None,
    )

    assert mgr.calls == ["get", "list_skills"]


@pytest.mark.asyncio
async def test_prepare_workspace_components_filters_search_skill(monkeypatch):
    """search_enabled=False → bocha_search 从 all_skills_meta 中剔除。"""
    monkeypatch.setattr(
        "app.services.orchestrator_service.WORKSPACE_BACKEND", "opensandbox"
    )
    mgr = _FakeWorkspaceManager(existing=_FakeSandbox())
    svc = _make_service(mgr)

    registry, _ = await svc._prepare_workspace_components(
        fused=FUSED, user_id="u1", redis_client=None,
        user_id_safe="u1", session_id_safe="s1",
        search_enabled=False, skills=[], langfuse_service=None,
    )

    names = [m.name for m in registry._all_skills_meta]
    assert "bocha_search" not in names
    assert "mineru" in names


def test_build_request_components_is_gone():
    """旧的合并方法必须删除，避免两条并存的装配路径。"""
    assert not hasattr(OrchestratorService, "_build_request_components")
