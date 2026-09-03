"""登录返回 agent_access 附带 description（取自 /api/intents 意图 definition）的测试。

覆盖：
- mng_service.build_agent_definition_map 的映射构建规则
- auth._build_auth_success 对 agent_access 的 description 注入与 Redis 持久化
- orchestrator 缺失 / 融合失败时的降级行为
"""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.routes.auth import _build_auth_success, _enrich_agent_access
from app.services.mng_service import build_agent_definition_map


# ---------- build_agent_definition_map ----------

class TestBuildAgentDefinitionMap:
    def test_normal_mapping(self):
        intents = [
            {
                "id": 123,
                "name": "生成PPT",
                "intentCode": "generate_ppt",
                "definition": "用于生成精美ppt",
                "agent": {"id": "999", "name": "生成PPT智能体"},
                "skills": [],
            },
            {
                "id": 124,
                "name": "生成表格",
                "intentCode": "generate_table",
                "definition": "用于生成表格",
                "agent": {"id": "888", "name": "表格智能体"},
                "skills": [],
            },
        ]
        assert build_agent_definition_map(intents) == {
            "999": "用于生成精美ppt",
            "888": "用于生成表格",
        }

    def test_first_definition_wins_for_same_agent(self):
        intents = [
            {"definition": "第一个", "agent": {"id": "1"}},
            {"definition": "第二个", "agent": {"id": "1"}},
        ]
        assert build_agent_definition_map(intents) == {"1": "第一个"}

    def test_skip_invalid_entries(self):
        intents = [
            "not-a-dict",
            {"definition": "无 agent"},
            {"agent": {"id": "2"}},
            {"agent": {"id": ""}, "definition": "空 id"},
            {"agent": "not-a-dict", "definition": "agent 非 dict"},
            {"agent": {"id": "3"}, "definition": ""},
        ]
        assert build_agent_definition_map(intents) == {}

    def test_empty_input(self):
        assert build_agent_definition_map([]) == {}
        assert build_agent_definition_map(None) == {}


# ---------- _enrich_agent_access ----------

class TestEnrichAgentAccess:
    def test_enrich_matched_only(self):
        permissions = {
            "agent_whitelist": [
                {"id": "123", "name": "制度问答", "show": 1},
                {"id": "999", "name": "生成PPT智能体", "show": 0},
            ],
            "skill_blacklist": [],
        }
        original = permissions["agent_whitelist"][0]
        _enrich_agent_access(permissions, {"999": "用于生成精美ppt"})
        enriched = permissions["agent_whitelist"]
        # 匹配项注入 description
        assert enriched[1]["description"] == "用于生成精美ppt"
        assert enriched[1]["name"] == "生成PPT智能体"
        # 未匹配项保持原对象（无 description 字段）
        assert enriched[0] is original
        assert "description" not in enriched[0]

    def test_noop_on_empty_definitions(self):
        permissions = {"agent_whitelist": [{"id": "1"}]}
        _enrich_agent_access(permissions, {})
        assert permissions["agent_whitelist"] == [{"id": "1"}]

    def test_noop_on_invalid_whitelist(self):
        permissions = {"agent_whitelist": "not-a-list"}
        _enrich_agent_access(permissions, {"1": "d"})
        assert permissions["agent_whitelist"] == "not-a-list"


# ---------- _build_auth_success 集成 ----------

_RESULT = {
    "verification": True,
    "user_info": {
        "id": "u1",
        "username": "zhangsan",
        "name": "小张",
        "department": "后勤部",
        "role": "普通用户",
    },
    "permissions": {
        "agent_whitelist": [
            {"id": "123", "name": "制度问答", "show": 1},
            {"id": "999", "name": "生成PPT智能体", "show": 0},
        ],
        "skill_blacklist": [],
    },
}

_FUSED = {
    "merged_intents": [],
    "merged_agents": [],
    "merged_skills": [],
    "default_orchestration": {},
    "agent_definitions": {"999": "用于生成精美ppt"},
}


def _make_request(orchestrator=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                redis_client=AsyncMock(),
                orchestrator_service=orchestrator,
            )
        )
    )


def _make_orchestrator(fused=None, exc=None):
    async def build_and_cache_user_config(**kwargs):
        if exc:
            raise exc
        return fused
    return SimpleNamespace(build_and_cache_user_config=build_and_cache_user_config)


class TestBuildAuthSuccess:
    @pytest.mark.asyncio
    async def test_agent_access_with_description(self):
        orch = _make_orchestrator(fused=_FUSED)
        request = _make_request(orchestrator=orch)
        with patch("app.routes.auth.fire_notify_mng_active"), \
             patch("app.routes.auth.save_user_permissions", new=AsyncMock()) as save_mock:
            resp = await _build_auth_success(copy.deepcopy(_RESULT), request)

        agent_access = resp["data"]["agent_access"]
        by_id = {a["id"]: a for a in agent_access}
        assert by_id["999"]["description"] == "用于生成精美ppt"
        assert "description" not in by_id["123"]

        # 存 Redis 的是已 enrich 的 permissions
        saved = save_mock.await_args.args[2]
        saved_by_id = {a["id"]: a for a in saved["agent_whitelist"]}
        assert saved_by_id["999"]["description"] == "用于生成精美ppt"

    @pytest.mark.asyncio
    async def test_degrade_when_orchestrator_missing(self):
        request = _make_request(orchestrator=None)
        with patch("app.routes.auth.fire_notify_mng_active"), \
             patch("app.routes.auth.save_user_permissions", new=AsyncMock()):
            resp = await _build_auth_success(copy.deepcopy(_RESULT), request)

        agent_access = resp["data"]["agent_access"]
        assert len(agent_access) == 2
        assert all("description" not in a for a in agent_access)

    @pytest.mark.asyncio
    async def test_degrade_when_fuse_fails(self):
        orch = _make_orchestrator(exc=RuntimeError("mng down"))
        request = _make_request(orchestrator=orch)
        with patch("app.routes.auth.fire_notify_mng_active"), \
             patch("app.routes.auth.save_user_permissions", new=AsyncMock()) as save_mock:
            resp = await _build_auth_success(copy.deepcopy(_RESULT), request)

        agent_access = resp["data"]["agent_access"]
        assert all("description" not in a for a in agent_access)
        # 权限仍正常保存（未 enrich）
        saved = save_mock.await_args.args[2]
        assert all("description" not in a for a in saved["agent_whitelist"])
