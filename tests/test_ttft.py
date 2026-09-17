"""P1-5 TTFT 计时单测：验证辅助函数正确识别首个内容事件并计时。"""
from app.services.chat_service import _compute_ttft_marker


def test_first_content_event_marks_ttft():
    """辅助：给定事件类型序列，首个 TEXT_BLOCK_DELTA/summary 触发 TTFT。"""
    assert _compute_ttft_marker("TEXT_BLOCK_DELTA") is True
    assert _compute_ttft_marker("summary") is True
    assert _compute_ttft_marker("react_step") is False
    assert _compute_ttft_marker("task_start") is False
