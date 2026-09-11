"""langfuse_service 扩展参数测试。"""
from unittest.mock import MagicMock

from app.services.langfuse_service import LangfuseService


def _make_enabled_service():
    """创建一个 _enabled=True、_client=mock 的实例（跳过真实初始化）。"""
    svc = LangfuseService.__new__(LangfuseService)
    svc._enabled = True
    svc._client = MagicMock()
    return svc


def test_start_observation_passes_model():
    svc = _make_enabled_service()
    svc._client.start_observation.return_value = MagicMock()

    svc.start_observation(name="llm-x", as_type="generation", model="deepseek-v4")
    kwargs = svc._client.start_observation.call_args.kwargs
    assert kwargs["model"] == "deepseek-v4"


def test_start_observation_passes_metadata():
    svc = _make_enabled_service()
    svc._client.start_observation.return_value = MagicMock()

    svc.start_observation(name="tool-x", as_type="tool", metadata={"k": "v"})
    kwargs = svc._client.start_observation.call_args.kwargs
    assert kwargs["metadata"] == {"k": "v"}


def test_end_observation_passes_usage_details():
    svc = _make_enabled_service()
    obs = MagicMock()

    svc.end_observation(obs, output="text", usage_details={"input": 100, "output": 50})
    kwargs = obs.update.call_args.kwargs
    assert kwargs["usage_details"]["input"] == 100
    assert kwargs["usage_details"]["output"] == 50
    obs.end.assert_called_once()


def test_end_observation_passes_level():
    svc = _make_enabled_service()
    obs = MagicMock()

    svc.end_observation(obs, output={"error": "boom"}, level="ERROR",
                        status_message="ValueError")
    kwargs = obs.update.call_args.kwargs
    assert kwargs["level"] == "ERROR"
    assert kwargs["status_message"] == "ValueError"


def test_end_observation_backward_compatible():
    """旧调用方只传 output，不传新参数，不应报错。"""
    svc = _make_enabled_service()
    obs = MagicMock()

    svc.end_observation(obs, output="text")
    obs.update.assert_called_once()
    obs.end.assert_called_once()


def test_start_observation_disabled_returns_none():
    svc = LangfuseService.__new__(LangfuseService)
    svc._enabled = False
    svc._client = None
    assert svc.start_observation(name="x") is None


def test_get_current_langfuse_returns_none_by_default():
    import app.services.langfuse_service as mod
    mod._current_instance = None
    assert mod.get_current_langfuse() is None


def test_set_and_get_current_langfuse():
    import app.services.langfuse_service as mod
    mod._current_instance = None
    svc = _make_enabled_service()
    mod.set_current_langfuse(svc)
    assert mod.get_current_langfuse() is svc
    mod._current_instance = None  # cleanup
