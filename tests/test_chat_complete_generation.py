"""P1-1 chat_complete generation 埋点测试。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.intent.llm_client import chat_complete


def _mock_openai_response(text="hello", prompt_tokens=100, completion_tokens=50):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    resp.usage = MagicMock(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return resp


def _mock_client(resp):
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_generation_records_real_usage():
    client = _mock_client(_mock_openai_response("答案", 123, 45))
    lf = MagicMock()
    lf.enabled = True
    obs = MagicMock()
    lf.start_observation.return_value = obs

    with patch("app.intent.llm_client.get_current_langfuse", return_value=lf):
        text = await chat_complete(
            client, {"model_name": "deepseek-v4"},
            system_prompt="sys", user_prompt="usr", stage="llm-query-rewrite",
        )

    assert text == "答案"
    lf.start_observation.assert_called_once()
    start_kwargs = lf.start_observation.call_args.kwargs
    assert start_kwargs["as_type"] == "generation"
    assert start_kwargs["name"] == "llm-query-rewrite"
    assert start_kwargs["model"] == "deepseek-v4"
    end_kwargs = lf.end_observation.call_args.kwargs
    assert end_kwargs["usage_details"] == {"input": 123, "output": 45}


@pytest.mark.asyncio
async def test_no_langfuse_does_not_crash():
    client = _mock_client(_mock_openai_response("x"))
    with patch("app.intent.llm_client.get_current_langfuse", return_value=None):
        text = await chat_complete(client, {"model_name": "m"},
                                   system_prompt="s", user_prompt="u")
    assert text == "x"


@pytest.mark.asyncio
async def test_serial_injected_when_set():
    """对话链路内（ContextVar 已设置）请求携带 app_serial_number 头。"""
    from app.services.trace_serial import set_serial
    client = _mock_client(_mock_openai_response("ok"))
    set_serial("OIA-AGENTSCOPE-testpair")
    try:
        await chat_complete(client, {"model_name": "m"},
                            system_prompt="s", user_prompt="u")
    finally:
        set_serial("")
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_headers"] == {"app_serial_number": "OIA-AGENTSCOPE-testpair"}


@pytest.mark.asyncio
async def test_serial_omitted_when_not_set():
    """非对话链路（ContextVar 为空）不带该头，保持原行为。"""
    from app.services.trace_serial import set_serial
    client = _mock_client(_mock_openai_response("ok"))
    set_serial("")
    await chat_complete(client, {"model_name": "m"},
                        system_prompt="s", user_prompt="u")
    kwargs = client.chat.completions.create.call_args.kwargs
    assert "extra_headers" not in kwargs


@pytest.mark.asyncio
async def test_exception_records_error_level():
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=ValueError("boom"))
    lf = MagicMock()
    lf.enabled = True
    obs = MagicMock()
    lf.start_observation.return_value = obs

    with patch("app.intent.llm_client.get_current_langfuse", return_value=lf):
        with pytest.raises(ValueError, match="boom"):
            await chat_complete(client, {"model_name": "m"},
                                system_prompt="s", user_prompt="u")

    end_kwargs = lf.end_observation.call_args.kwargs
    assert end_kwargs["level"] == "ERROR"
    assert end_kwargs["status_message"] == "ValueError"
