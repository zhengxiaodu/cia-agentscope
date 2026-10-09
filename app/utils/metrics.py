"""指标 stub：P0 阶段空实现，P2 替换为真实 Prometheus 指标。

所有方法均不抛异常，确保 traced_stage 的 try/except 不会因缺实现而告警。
"""


def record_stage(name: str, duration_ms: int, success: bool) -> None:
    """记录一个环节的耗时与成败。P0 阶段空实现。"""
    pass
