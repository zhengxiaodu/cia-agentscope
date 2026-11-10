"""trace_context 单元测试。"""
from app.utils.trace_context import (
    set_request_context,
    clear_request_context,
    get_session_id,
    get_user_id,
    get_trace_id,
    get_span_id,
)


def test_session_id_set_and_get():
    set_request_context("sess-123", "user-456")
    try:
        assert get_session_id() == "sess-123"
        assert get_user_id() == "user-456"
    finally:
        clear_request_context()


def test_clear_request_context():
    set_request_context("sess-123", "user-456")
    clear_request_context()
    assert get_session_id() is None
    assert get_user_id() is None


def test_get_trace_id_returns_none_without_span():
    """无活跃 OTel span 时返回 None。"""
    assert get_trace_id() is None


def test_get_span_id_returns_none_without_span():
    assert get_span_id() is None


def test_set_request_context_ignores_none():
    """传 None 不覆盖已有值。"""
    set_request_context("sess-1", "user-1")
    try:
        set_request_context(None, None)
        assert get_session_id() == "sess-1"
        assert get_user_id() == "user-1"
    finally:
        clear_request_context()
