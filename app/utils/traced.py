"""统一埋点入口：一次 with 同时产出 Langfuse observation、结构化日志与指标。"""
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)


class StageRecorder:
    """埋点句柄：业务代码通过它写 output 与扩展字段。"""

    def __init__(self, observation: Any) -> None:
        self._obs = observation
        self._extra: dict = {}
        self.success = True

    def set_output(self, **fields: Any) -> None:
        self._extra.update(fields)

    def mark_degraded(self, reason: str, detail: str = "") -> None:
        """记录降级：降级率与降级原因分布是意图链路的关键指标。"""
        self.success = False
        self._extra.update({"degraded": True, "reason": reason, "detail": detail[:500]})


@contextmanager
def traced_stage(
    langfuse_service: Optional[Any],
    name: str,
    input: Any = None,
    as_type: str = "span",
) -> Iterator[StageRecorder]:
    """环节埋点。退出时自动写 duration_ms/success，异常自动归因并重抛。

    观测失败绝不影响业务：langfuse 未启用时退化为纯日志+指标。
    """
    started = time.perf_counter()
    obs = None
    if langfuse_service is not None:
        obs = langfuse_service.start_observation(name=name, as_type=as_type, input=input)
    recorder = StageRecorder(obs)
    try:
        yield recorder
    except Exception as e:
        recorder.success = False
        recorder._extra.update({
            "errorClass": f"{type(e).__module__}.{type(e).__name__}",
            "errorMessage": str(e)[:500],
        })
        raise
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        payload = {"stage": name, "durationMs": duration_ms,
                   "success": recorder.success, **recorder._extra}
        try:
            if langfuse_service is not None and obs is not None:
                langfuse_service.end_observation(obs, output=payload)
        except Exception:
            logger.debug("[traced_stage] observation 收尾失败", exc_info=True)
        logger.info("[stage] %s %dms", name, duration_ms, extra={"biz": payload})
        try:
            from app.utils.metrics import record_stage
            record_stage(name, duration_ms, recorder.success)
        except Exception:
            pass
