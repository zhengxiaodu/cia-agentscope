"""traced_stage 包装器单元测试。"""
from unittest.mock import MagicMock

import pytest

from app.utils.traced import traced_stage, StageRecorder


def test_traced_stage_records_duration_and_success():
    lf = MagicMock()
    lf.enabled = True
    obs = MagicMock()
    lf.start_observation.return_value = obs

    with traced_stage(lf, "test-stage") as rec:
        rec.set_output(result="ok")

    lf.start_observation.assert_called_once()
    lf.end_observation.assert_called_once()
    end_kwargs = lf.end_observation.call_args.kwargs
    assert end_kwargs["output"]["stage"] == "test-stage"
    assert end_kwargs["output"]["success"] is True
    assert "durationMs" in end_kwargs["output"]
    assert end_kwargs["output"]["result"] == "ok"


def test_traced_stage_records_exception():
    lf = MagicMock()
    lf.enabled = True
    obs = MagicMock()
    lf.start_observation.return_value = obs

    with pytest.raises(ValueError, match="boom"):
        with traced_stage(lf, "test-stage"):
            raise ValueError("boom")

    end_kwargs = lf.end_observation.call_args.kwargs
    assert end_kwargs["output"]["success"] is False
    assert "errorClass" in end_kwargs["output"]
    assert "errorMessage" in end_kwargs["output"]


def test_traced_stage_works_without_langfuse():
    """langfuse_service 为 None 时退化为纯日志，不报错。"""
    with traced_stage(None, "test-stage") as rec:
        rec.set_output(x=1)
    # 无 crash 即通过


def test_traced_stage_mark_degraded():
    lf = MagicMock()
    lf.enabled = True
    obs = MagicMock()
    lf.start_observation.return_value = obs

    with traced_stage(lf, "test-stage") as rec:
        rec.mark_degraded(reason="TimeoutError", detail="LLM timeout")

    end_kwargs = lf.end_observation.call_args.kwargs
    assert end_kwargs["output"]["success"] is False
    assert end_kwargs["output"]["degraded"] is True
    assert end_kwargs["output"]["reason"] == "TimeoutError"
