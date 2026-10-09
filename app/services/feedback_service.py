"""卡片反馈挂起注册表。

为 render_feedback_card 工具提供同轮会话内的「发卡 → 挂起 → 回传 → 恢复」能力：
工具发卡后在此注册 pending 并 await，POST /chat/feedback 收到用户反馈后
resolve 唤醒工具，agent 在同一轮 ReAct 循环内继续推理。

按 session 单槽：同一会话流同时只允许一个挂起反馈，防止 agent 在
上一个反馈未回收时连续发卡导致反馈错配。原型阶段为进程内状态，
不做持久化（断线/重启即丢，前端点击将得到 404 并禁用卡片）。
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 挂起超时（秒），本地开发可通过环境变量覆盖（验证超时路径时可调小）
FEEDBACK_TIMEOUT_SECONDS = float(os.getenv("FEEDBACK_TIMEOUT_SECONDS", "300"))


@dataclass
class PendingFeedback:
    feedback_id: str
    session_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)


_pending: Dict[str, PendingFeedback] = {}


async def wait_feedback(session_id: str, feedback_id: str,
                        timeout: Optional[float] = None) -> Dict[str, Any]:
    """注册挂起并等待用户反馈（阻塞直至 resolve/cancel/超时）。

    Returns:
        resolve 传入的 {"action","payload"}；或 {"status","message"} 形态的
        timeout / cancelled / error 结果（由 agent 据此优雅收尾）。
    """
    if timeout is None:
        timeout = FEEDBACK_TIMEOUT_SECONDS
    if session_id in _pending:
        logger.warning("[feedback_service] session=%s 已有挂起反馈，拒绝二次注册", session_id)
        return {"status": "error", "message": "上一反馈未完成，请先等待用户反馈后再发下一张卡"}
    pending = PendingFeedback(feedback_id=feedback_id, session_id=session_id)
    _pending[session_id] = pending
    logger.info("[feedback_service] 挂起等待反馈 session=%s feedback_id=%s timeout=%s",
                session_id, feedback_id, timeout)
    try:
        await asyncio.wait_for(pending.event.wait(), timeout=timeout)
        if pending.result is None:
            return {"status": "cancelled", "message": "会话已结束，反馈被取消"}
        return pending.result
    except asyncio.TimeoutError:
        logger.warning("[feedback_service] 反馈超时 session=%s feedback_id=%s", session_id, feedback_id)
        return {"status": "timeout", "message": f"用户未在 {int(timeout)} 秒内反馈"}
    finally:
        _pending.pop(session_id, None)


def resolve_feedback(session_id: str, feedback_id: str, result: Dict[str, Any]) -> bool:
    """回传端点调用：填充结果并唤醒挂起的 wait。

    Returns:
        True=已唤醒；False=不存在或 feedback_id 不匹配（404 语义）。
    """
    pending = _pending.get(session_id)
    if pending is None or pending.feedback_id != feedback_id:
        return False
    if pending.result is not None:
        return False
    pending.result = result
    pending.event.set()
    logger.info("[feedback_service] 反馈已回传 session=%s feedback_id=%s", session_id, feedback_id)
    return True


def cancel_feedback(session_id: str) -> None:
    """会话流终止（正常结束/中断/stop）时清理挂起，唤醒 wait 走 cancelled 分支。"""
    pending = _pending.pop(session_id, None)
    if pending is not None:
        logger.info("[feedback_service] 清理挂起反馈 session=%s feedback_id=%s",
                    session_id, pending.feedback_id)
        pending.event.set()
