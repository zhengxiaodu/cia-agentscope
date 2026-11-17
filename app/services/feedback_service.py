"""卡片反馈单槽：Redis 保存身份及结果，发卡进程轮询后恢复工具。

configure_feedback 在应用启动时配置一次；None 保留单进程开发模式。
每次挂起捕获自己的 Redis 客户端和随机身份，旧流清理不会删除新卡。
"""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional
import uuid

logger = logging.getLogger(__name__)
FEEDBACK_TIMEOUT_SECONDS = float(os.getenv("FEEDBACK_TIMEOUT_SECONDS", "300"))
FEEDBACK_POLL_INTERVAL_SECONDS = 0.25
_FEEDBACK_TTL_GRACE_SECONDS = 30
_redis_client: Any = None

_RESOLVE_SCRIPT = """
-- feedback:resolve
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local state = cjson.decode(raw)
if state.feedback_id ~= ARGV[1] or state.status ~= 'pending' then return 0 end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then return 0 end
state.status = 'resolved'
-- Keep result JSON opaque: Lua cjson round-trips can change empty arrays/objects.
state.result_json = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(state), 'PX', ttl)
return 1
"""

_CONSUME_SCRIPT = """
-- feedback:consume
local raw = redis.call('GET', KEYS[1])
if not raw then return false end
local state = cjson.decode(raw)
if state.owner_token ~= ARGV[1] then return false end
redis.call('DEL', KEYS[1])
return raw
"""


class FeedbackBackendError(RuntimeError):
    """共享反馈存储不可用；配置 Redis 后不退回本地接受反馈。"""


class FeedbackAlreadyPending(RuntimeError):
    """同一会话已有一张等待用户提交的卡片。"""


@dataclass
class PendingFeedback:
    feedback_id: str
    session_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    owner_token: str = field(default_factory=lambda: uuid.uuid4().hex)
    redis_client: Any = None
    timeout: float = FEEDBACK_TIMEOUT_SECONDS
    deadline: float = 0.0


_pending: Dict[str, PendingFeedback] = {}


def configure_feedback(redis_client: Any) -> None:
    """由 lifespan 注入共享 Redis；已有挂起保持注册时捕获的客户端。"""
    global _redis_client
    _redis_client = redis_client


def _key(session_id: str) -> str:
    return f"chat_feedback:{session_id}"


async def _redis_call(client: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    task = asyncio.current_task()
    cancellations = task.cancelling()
    try:
        result = await asyncio.wait_for(getattr(client, method)(*args, **kwargs), timeout=5)
        # wait_for can race with an already completed Redis call and swallow
        # external cancellation on Python 3.11. Preserve it, but allow cleanup
        # calls entered after cancellation to consume their own registration.
        if task.cancelling() > cancellations:
            raise asyncio.CancelledError()
        return result
    except Exception as exc:
        raise FeedbackBackendError("共享反馈存储暂不可用，请稍后重试") from exc


def _decode(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None or raw is False:
        return None
    try:
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError("feedback record must be an object")
        return state
    except (TypeError, ValueError) as exc:
        raise FeedbackBackendError("共享反馈记录格式异常") from exc


def _forget_local(pending: PendingFeedback) -> None:
    if _pending.get(pending.session_id) is pending:
        del _pending[pending.session_id]


async def _consume(pending: PendingFeedback) -> Optional[Dict[str, Any]]:
    raw = await _redis_call(
        pending.redis_client, "eval", _CONSUME_SCRIPT, 1,
        _key(pending.session_id), pending.owner_token,
    )
    return _decode(raw)


@asynccontextmanager
async def feedback_registration(
    session_id: str, feedback_id: str, timeout: Optional[float] = None,
) -> AsyncIterator[PendingFeedback]:
    """先注册再发卡；退出（含生成器关闭、取消、异常）按身份回收。"""
    timeout = FEEDBACK_TIMEOUT_SECONDS if timeout is None else float(timeout)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("feedback timeout must be a finite non-negative number")
    if session_id in _pending:
        raise FeedbackAlreadyPending("上一反馈未完成，请先等待用户反馈后再发下一张卡")
    pending = PendingFeedback(
        feedback_id=feedback_id, session_id=session_id, redis_client=_redis_client,
        timeout=timeout, deadline=asyncio.get_running_loop().time() + timeout,
    )
    # Reserve locally before awaiting Redis so same-process concurrent registrations
    # cannot both proceed. Redis NX also serializes registrations from other Pods.
    _pending[session_id] = pending
    body_failed = False
    try:
        if pending.redis_client is not None:
            state = json.dumps({
                "feedback_id": feedback_id, "owner_token": pending.owner_token,
                "status": "pending",
            })
            registered = await _redis_call(
                pending.redis_client, "set", _key(session_id), state, nx=True,
                ex=max(1, math.ceil(timeout) + _FEEDBACK_TTL_GRACE_SECONDS),
            )
            if not registered:
                raise FeedbackAlreadyPending("上一反馈未完成，请先等待用户反馈后再发下一张卡")
        yield pending
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            if pending.redis_client is not None:
                # Also safe after SET failure/timeout: a random identity can only
                # consume its own record, never another registration's slot.
                await _consume(pending)
        except FeedbackBackendError:
            if not body_failed:
                raise
            logger.exception("[feedback_service] 反馈清理失败 session=%s", session_id)
        finally:
            _forget_local(pending)


def _cancelled_result() -> Dict[str, Any]:
    return {"status": "cancelled", "message": "会话已结束，反馈被取消"}


def _timeout_result(timeout: float) -> Dict[str, Any]:
    return {"status": "timeout", "message": f"用户未在 {int(timeout)} 秒内反馈"}


def _resolved_result(state: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = json.loads(state["result_json"])
        if not isinstance(result, dict):
            raise ValueError("feedback result must be an object")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise FeedbackBackendError("共享反馈结果格式异常") from exc


async def wait_registered_feedback(pending: PendingFeedback) -> Dict[str, Any]:
    """等待已注册卡片；Redis 结果持久保存，回传早于轮询也不会丢失。"""
    loop = asyncio.get_running_loop()
    if pending.redis_client is None:
        if pending.event.is_set():
            return pending.result if pending.result is not None else _cancelled_result()
        try:
            await asyncio.wait_for(pending.event.wait(), max(0, pending.deadline - loop.time()))
        except asyncio.TimeoutError:
            return _timeout_result(pending.timeout)
        return pending.result if pending.result is not None else _cancelled_result()

    while True:
        state = _decode(await _redis_call(pending.redis_client, "get", _key(pending.session_id)))
        if state is None or state.get("owner_token") != pending.owner_token:
            return _cancelled_result()
        if state.get("status") == "resolved":
            return _resolved_result(state)
        remaining = pending.deadline - loop.time()
        if remaining <= 0:
            # Atomically close the slot and inspect its final state: a callback
            # accepted just before the deadline must still reach its owner.
            final_state = await _consume(pending)
            if final_state is None:
                return _cancelled_result()
            if final_state.get("status") == "resolved":
                return _resolved_result(final_state)
            return _timeout_result(pending.timeout)
        try:
            await asyncio.wait_for(
                pending.event.wait(), min(FEEDBACK_POLL_INTERVAL_SECONDS, remaining),
            )
        except asyncio.TimeoutError:
            pass


async def wait_feedback(
    session_id: str, feedback_id: str, timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """兼容直接调用；需要向前端发卡的工具应先 feedback_registration。"""
    try:
        async with feedback_registration(session_id, feedback_id, timeout) as pending:
            return await wait_registered_feedback(pending)
    except FeedbackAlreadyPending as exc:
        return {"status": "error", "message": str(exc)}


def resolve_feedback(session_id: str, feedback_id: str, result: Dict[str, Any]) -> bool:
    """单进程兼容入口；共享模式必须 await resolve_feedback_shared。"""
    pending = _pending.get(session_id)
    if _redis_client is not None or (pending and pending.redis_client is not None):
        raise FeedbackBackendError("共享反馈模式请使用 resolve_feedback_shared")
    if pending is None or pending.feedback_id != feedback_id or pending.result is not None:
        return False
    if pending.event.is_set():
        return False
    pending.result = result
    pending.event.set()
    return True


async def resolve_feedback_shared(
    session_id: str, feedback_id: str, result: Dict[str, Any],
) -> bool:
    """任意 Pod 回传：Lua 校验 feedback_id 并原子接受一次，保留原 TTL。"""
    client = _redis_client
    if client is None:
        return resolve_feedback(session_id, feedback_id, result)
    accepted = await _redis_call(
        client, "eval", _RESOLVE_SCRIPT, 1, _key(session_id), feedback_id,
        json.dumps(result, ensure_ascii=True),
    )
    return bool(accepted)


def cancel_feedback(session_id: str) -> None:
    """单进程兼容入口；共享模式必须 await cancel_feedback_shared。"""
    pending = _pending.get(session_id)
    if pending is None:
        return
    if pending.redis_client is not None:
        raise FeedbackBackendError("共享反馈模式请使用 cancel_feedback_shared")
    _forget_local(pending)
    pending.event.set()


async def cancel_feedback_shared(session_id: str) -> None:
    """流结束时仅清理本进程注册的身份，不按 session 删除远端新卡。"""
    pending = _pending.get(session_id)
    if pending is None:
        return
    if pending.redis_client is not None:
        await _consume(pending)
    _forget_local(pending)
    pending.event.set()
