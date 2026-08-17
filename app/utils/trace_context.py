"""链路追踪上下文：请求级观测元信息的隐式传递。

设计要点：
- trace_id / span_id 不自行生成、不落 contextvars，统一从 OpenTelemetry
  当前 span 读取。langfuse 4.x 基于 OTel 构建，其 trace_id 与 OTel
  span_context.trace_id 同源（已实测一致），因此日志与 Langfuse trace
  天然可双向跳转，无需把 langfuse_service 逐层透传。
- session_id / user_id 在请求入口注入一次，全链路就地读取。
"""
import contextvars
from typing import Optional

from opentelemetry import trace as otel_trace

_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_session_id", default=None
)
_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_user_id", default=None
)


def set_request_context(session_id: Optional[str], user_id: Optional[str]) -> None:
    """请求入口注入业务上下文。"""
    if session_id:
        _session_id.set(session_id)
    if user_id:
        _user_id.set(user_id)


def clear_request_context() -> None:
    """请求结束清理，避免异步上下文串味。"""
    _session_id.set(None)
    _user_id.set(None)


def get_session_id() -> Optional[str]:
    return _session_id.get()


def get_user_id() -> Optional[str]:
    return _user_id.get()


def get_trace_id() -> Optional[str]:
    """取当前 trace_id（32 位 hex），与 Langfuse observation.trace_id 一致。"""
    try:
        ctx = otel_trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return None


def get_span_id() -> Optional[str]:
    """取当前 span_id（16 位 hex）。"""
    try:
        ctx = otel_trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.span_id, "016x")
    except Exception:
        pass
    return None
