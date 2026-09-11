"""logging_setup 单元测试。"""
import json
import logging

from app.utils.logging_setup import TraceJsonFormatter
from app.utils.trace_context import set_request_context, clear_request_context


def _make_record(msg="hello %s", args=("world",), extra=None):
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="/t.py",
        lineno=1, msg=msg, args=args, exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


def test_formatter_outputs_valid_json():
    formatter = TraceJsonFormatter()
    output = formatter.format(_make_record())
    data = json.loads(output)
    assert data["message"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "test.logger"
    assert "@timestamp" in data


def test_formatter_includes_session_id_when_set():
    set_request_context("sess-test", "user-test")
    try:
        formatter = TraceJsonFormatter()
        output = formatter.format(_make_record())
        data = json.loads(output)
        assert data["sessionId"] == "sess-test"
        assert data["userId"] == "user-test"
    finally:
        clear_request_context()


def test_formatter_includes_biz_extra():
    formatter = TraceJsonFormatter()
    record = _make_record(extra={"biz": {"stage": "query-rewrite", "durationMs": 42}})
    output = formatter.format(record)
    data = json.loads(output)
    assert data["biz"]["stage"] == "query-rewrite"
    assert data["biz"]["durationMs"] == 42


def test_formatter_omits_trace_fields_when_no_context():
    formatter = TraceJsonFormatter()
    output = formatter.format(_make_record())
    data = json.loads(output)
    assert "traceId" not in data
    assert "sessionId" not in data
