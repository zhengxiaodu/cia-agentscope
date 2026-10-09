"""日志初始化：JSON 结构化输出 + trace 上下文自动注入 + 级别可配。

解决三件事：
1. 无 basicConfig 导致默认 WARNING+ 生效，线上所有 logger.info 静默丢弃；
2. 日志无 trace_id，无法与 Langfuse trace 互跳，并发下多条日志无法归组；
3. 无 JSON schema、无落盘，无法被日志系统采集聚合。
"""
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone

from app.utils.trace_context import (
    get_session_id, get_span_id, get_trace_id, get_user_id,
)


class TraceJsonFormatter(logging.Formatter):
    """一行一个 JSON，自动注入 trace 上下文字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "@timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in (
            ("traceId", get_trace_id()),
            ("spanId", get_span_id()),
            ("sessionId", get_session_id()),
            ("userId", get_user_id()),
        ):
            if value:
                payload[key] = value
        biz = getattr(record, "biz", None)
        if isinstance(biz, dict):
            payload["biz"] = biz
        if record.exc_info:
            payload["stackTrace"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            payload["biz"] = str(biz)
            return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """在 lifespan 最开始调用。控制台保留可读文本，文件输出 JSON。"""
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))
    root.addHandler(console)

    log_dir = os.getenv("LOG_DIR", "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "observability.json"),
            maxBytes=int(os.getenv("LOG_MAX_BYTES", str(64 * 1024 * 1024))),
            backupCount=int(os.getenv("LOG_BACKUP_COUNT", "7")),
            encoding="utf-8",
        )
        file_handler.setFormatter(TraceJsonFormatter())
        root.addHandler(file_handler)
    except Exception:
        root.warning("JSON 日志文件初始化失败，仅保留控制台输出", exc_info=True)

    for noisy in ("httpx", "httpcore", "urllib3", "docker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
