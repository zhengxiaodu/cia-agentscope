"""trace_names 单元测试。"""
from app.utils.trace_names import TraceName


def test_chat_response_value():
    assert TraceName.CHAT_RESPONSE == "chat-response"


def test_agent_run_format():
    assert TraceName.AGENT_RUN.format(agent_id="general") == "agent-general"


def test_react_step_format():
    assert TraceName.REACT_STEP.format(step=3) == "react-step-3"


def test_tool_call_format():
    assert TraceName.TOOL_CALL.format(tool_name="Bash") == "tool-Bash"


def test_all_values_are_strings():
    for name in TraceName:
        assert isinstance(name.value, str)
        assert "-" in name.value or name.value.count("-") >= 0  # kebab-case
