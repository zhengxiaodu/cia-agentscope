"""工具智能体绑定（agent_bound_tools）过滤测试。

验证：声明的工具只注入给列表内智能体；
未声明工具保持全局可见；配置为空时不过滤。
"""
from types import SimpleNamespace

from app.agents.registry import AgentRegistry


def _registry(agent_bound_tools=None) -> AgentRegistry:
    """绕过 __init__，仅设置过滤逻辑用到的属性。"""
    reg = AgentRegistry.__new__(AgentRegistry)
    reg._agent_bound_tools = {
        str(k): list(v or []) for k, v in (agent_bound_tools or {}).items()
    }
    return reg


def _tools(*names):
    return [SimpleNamespace(name=n) for n in names]


def _agent(agent_id, skills=None):
    return SimpleNamespace(id=agent_id, skills=skills or [])


def test_bound_tool_only_injected_to_bound_agent():
    """绑定工具只注入给列表内智能体。"""
    reg = _registry({"policy_qa": ["regulations_agent"]})
    tools = _tools("render_bar_chart", "policy_qa", "feedback")

    kept = reg._filter_tools_for_agent(_agent("regulations_agent"), tools)

    assert [t.name for t in kept] == ["render_bar_chart", "policy_qa", "feedback"]


def test_bound_tool_excluded_from_other_agents():
    """非绑定智能体拿不到绑定工具。"""
    reg = _registry({"policy_qa": ["regulations_agent"]})
    tools = _tools("render_bar_chart", "policy_qa", "feedback")

    kept = reg._filter_tools_for_agent(_agent("general_agent"), tools)

    assert [t.name for t in kept] == ["render_bar_chart", "feedback"]


def test_unbound_tools_visible_to_all_agents():
    """未声明的工具对所有智能体可见。"""
    reg = _registry({"policy_qa": ["regulations_agent"]})

    for agent_id in ("general_agent", "chart_agent", "regulations_agent"):
        kept = reg._filter_tools_for_agent(
            _agent(agent_id), _tools("render_bar_chart", "feedback", "md_to_docx")
        )
        assert [t.name for t in kept] == ["render_bar_chart", "feedback", "md_to_docx"]


def test_empty_config_keeps_all_tools():
    """配置为空 → 不过滤，行为与改动前一致。"""
    reg = _registry(None)
    tools = _tools("policy_qa", "render_bar_chart")

    kept = reg._filter_tools_for_agent(_agent("general_agent"), tools)

    assert [t.name for t in kept] == ["policy_qa", "render_bar_chart"]


def test_tool_bound_to_multiple_agents():
    """一个工具绑定多个智能体 → 列表内都可见。"""
    reg = _registry({"policy_qa": ["regulations_agent", "general_agent"]})
    tools = _tools("policy_qa")

    for agent_id in ("regulations_agent", "general_agent"):
        kept = reg._filter_tools_for_agent(_agent(agent_id), tools)
        assert [t.name for t in kept] == ["policy_qa"]
    kept = reg._filter_tools_for_agent(_agent("chart_agent"), tools)
    assert kept == []
