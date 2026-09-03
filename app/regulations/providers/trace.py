"""全链路追踪流水号 — app_serial_number。

每次用户问答在入口生成一次，写入 ContextVar；HttpxChatOpenAI 与标题生成从
context 读取并注入请求头。asyncio.create_task 复制 context，后台任务自动
继承同一流水号。
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar

# 组件前缀（9 位）：超 9 位截断，不足 9 位补 0
APP_SERIAL_COMPONENT = "oiapolyqa"

_current_serial: ContextVar[str] = ContextVar("app_serial_number", default="")


def new_serial() -> str:
    """生成 app_serial_number：9 位组件前缀 + 毫秒时间戳 + 随机后缀。"""
    comp = APP_SERIAL_COMPONENT.strip()[:9].ljust(9, "0")
    timestamp = str(int(time.time() * 1000))
    rand = uuid.uuid4().hex[:16]
    return f"{comp}{timestamp}{rand}"


def set_serial(serial: str) -> None:
    _current_serial.set(serial)


def get_serial() -> str:
    return _current_serial.get()
