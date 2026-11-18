"""显式触发智能体（explicit_only_agents）意图候选集过滤测试。

验证：标记智能体对应的意图被剔除出意图识别候选集；
未标记意图保留；集合为空时行为不变。
"""
import pytest

from app.services.orchestrator_service import OrchestratorService


def _make_service(explicit_only_agents=None) -> OrchestratorService:
    """绕过 __init__，仅设置 _build_intent_components 用到的属性。"""
    svc = OrchestratorService.__new__(OrchestratorService)
    svc._prompts = {
        "rewrite": "改写提示词",
        "intent_recognition": "识别提示词 {{intents}}",
        "intent_orchestration": "编排提示词 {{intents_list}}",
    }
    svc._intent_client = object()
    svc._intent_model_cfg = {"model": "qwen-plus"}
    svc._explicit_only_agents = set(explicit_only_agents or [])
    return svc


def _fused():
    """三个意图：通用（保留）、深度研究（标记剔除）、制度问答（保留）。"""
    return {
        "merged_intents": [
            {"id": "general_chat", "name": "通用问答", "description": "兜底", "agent": "general_agent"},
            {"id": "deep_research", "name": "深度研究", "description": "深度调研", "agent": "agent_deep_research"},
            {"id": "regulations_qa", "name": "制度问答", "description": "制度", "agent": "regulations_agent"},
        ],
        "merged_agents": [],
        "merged_skills": [],
        "default_orchestration": {},
    }


def _intent_ids(recognizer) -> list:
    """提取识别器实际拿到的意图候选 id 列表。"""
    return sorted(ic.id for ic in recognizer._intent_configs)


def test_explicit_only_intent_excluded_from_candidates():
    """标记智能体对应的意图不出现在意图识别候选集中。"""
    svc = _make_service(explicit_only_agents=["agent_deep_research"])

    _, recognizer = svc._build_intent_components(_fused())

    assert _intent_ids(recognizer) == ["general_chat", "regulations_qa"]


def test_empty_set_keeps_all_intents():
    """集合为空 → 候选集不过滤，行为与改动前一致。"""
    svc = _make_service(explicit_only_agents=[])

    _, recognizer = svc._build_intent_components(_fused())

    assert _intent_ids(recognizer) == ["deep_research", "general_chat", "regulations_qa"]


def test_multiple_agents_all_excluded():
    """同时标记多个智能体 → 对应意图全部剔除，其余保留。"""
    svc = _make_service(explicit_only_agents=["agent_deep_research", "regulations_agent"])

    _, recognizer = svc._build_intent_components(_fused())

    assert _intent_ids(recognizer) == ["general_chat"]


def test_recognition_prompt_contains_only_visible_intents():
    """渲染进意图识别 prompt 的意图清单不含被剔除意图的描述。"""
    svc = _make_service(explicit_only_agents=["agent_deep_research"])

    _, recognizer = svc._build_intent_components(_fused())

    assert "深度研究" not in recognizer._intents_desc
    assert "通用问答" in recognizer._intents_desc
