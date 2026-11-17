"""Ownership, cancellation and single-flight control for one chat round."""
import asyncio
import json
import uuid
from contextvars import ContextVar

from app.services.redis_lease import CoordinationUnavailable, LeaseLost, RedisLease, decode, transient_io_error

_STOP_POLL_INTERVAL = 0.5
_WORKSPACE_GATE_WAIT = 5
# 停止标志探测失败时指数退避；达到阈值且无已确认有效租约才中止，
# 已确认租约的有效期仍由独立续期任务约束。
_STOP_MAX_FAILURES = 5
_STOP_BACKOFF_CAP = 5.0
# Inherited by the SSE pump and workspace-preparation tasks, never global run state.
current_chat_run = ContextVar("current_chat_run", default=None)

_STOP_RUN = """-- chat_stop_run
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('set', KEYS[2], '1', 'EX', 300)
  return 1
end
return 0
"""


class CancellationEvent(asyncio.Event):
    """取消信号：携带取消来源，避免把所有中断都当成用户主动停止。

    asyncio.Event.set() 本身不接收参数；这里重写以记录首次置位的原因，
    供 chat 流在收尾时给出准确归因（user / lease_lost / redis_failure）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason: str | None = None

    def set(self, reason: str | None = None) -> None:  # type: ignore[override]
        if self.reason is None:
            self.reason = reason or "unknown"
        super().set()


class RunBusy(RuntimeError):
    pass


class ChatRun:
    def __init__(self, redis, session_id, user_id):
        self.redis, self.session_id, self.user_id = redis, session_id, str(user_id)
        self.run_id = uuid.uuid4().hex
        self.cancel_event = CancellationEvent()
        self.key = f"chat_run:{session_id}"
        self.stop_key = f"chat_stop:{session_id}:{self.run_id}"
        self.token = json.dumps({"run_id": self.run_id, "user_id": self.user_id}, sort_keys=True)
        # ttl 从默认 30s 放宽到 120s：LLM 流式输出、工具执行期间事件循环可能
        # 被同步阻塞，续期协程无法及时调度；120s 给阻塞 + Redis 抖动留足余量，
        # 避免正常对话被误判为 lease_lost 中断。
        self.lease = RedisLease(redis, self.key, token=self.token, ttl=120,
                                on_lost=lambda: self.cancel_event.set("lease_lost")) if redis is not None else None
        self._closed = False
        self._close_task = None

    async def open(self):
        if self.lease:
            # A retire operation holds this user-level gate while checking active
            # sessions. New sessions must register under the same gate.
            gate = RedisLease(self.redis, f"sandbox_lock:user-{self.user_id}",
                              wait_timeout=_WORKSPACE_GATE_WAIT)
            try:
                if not await gate.acquire():
                    raise RunBusy("该用户工作区正在准备，请稍后重试")
                if not await self.lease.acquire():
                    raise RunBusy("该会话正在生成，请等待当前轮结束")
                await gate.assert_owned()
            except BaseException:
                await self.lease.release()
                raise
            finally:
                await gate.release()
        current_chat_run.set((self.session_id, self.token))

    async def watch_stop(self):
        if self.redis is None:
            await self.cancel_event.wait()
            return
        failures = 0
        while not self.cancel_event.is_set():
            if self.lease is not None and self.lease.lost.is_set():
                self.cancel_event.set("lease_lost")
                return
            try:
                if await asyncio.wait_for(self.redis.exists(self.stop_key), timeout=5):
                    self.cancel_event.set("user")
                    return
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if not transient_io_error(exc) or (
                    failures >= _STOP_MAX_FAILURES
                    and (self.lease is None or not self.lease.locally_valid())
                ):
                    self.cancel_event.set("redis_failure")
                    return
                # 抖动容忍：指数退避后再探，避免 Redis 瞬时不可用直接中断本轮
                await asyncio.sleep(
                    min(_STOP_POLL_INTERVAL * (2 ** failures), _STOP_BACKOFF_CAP)
                )
                continue
            await asyncio.sleep(_STOP_POLL_INTERVAL)

    async def close(self):
        # ASGI disconnect cancellation must not strand a renewing lease forever.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)
        if current_chat_run.get() == (self.session_id, self.token):
            current_chat_run.set(None)

    async def _close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.redis is not None:
                try:
                    await asyncio.wait_for(self.redis.delete(self.stop_key), timeout=5)
                except Exception:
                    pass  # The round-specific key expires and cannot stop a later round.
        finally:
            if self.lease:
                await self.lease.release()


async def authorize_active_run(redis, session_id, user_id, local_run=None):
    if redis is None:
        if local_run is None:
            return None
        data = {"run_id": local_run.run_id, "user_id": local_run.user_id}
    else:
        try:
            raw = decode(await asyncio.wait_for(redis.get(f"chat_run:{session_id}"), timeout=5))
            if raw is None:
                return None
            data = json.loads(raw)
            if not isinstance(data, dict) or not data.get("run_id") or "user_id" not in data:
                raise ValueError("invalid active run")
        except Exception as exc:
            raise CoordinationUnavailable("无法读取会话运行状态") from exc
    if str(data["user_id"]) != str(user_id):
        raise PermissionError("无权操作该会话")
    return data


async def request_stop(redis, session_id, user_id, local_run=None):
    data = await authorize_active_run(redis, session_id, user_id, local_run)
    if data is None:
        return False
    if redis is not None:
        token = json.dumps(data, sort_keys=True)
        try:
            stopped = await asyncio.wait_for(redis.eval(
                _STOP_RUN, 2, f"chat_run:{session_id}",
                f"chat_stop:{session_id}:{data['run_id']}", token), timeout=5)
        except Exception as exc:
            raise CoordinationUnavailable("无法发送停止信号") from exc
        if not stopped:
            return False
    if local_run is not None and local_run.run_id == data["run_id"]:
        local_run.cancel_event.set("user")
    return True
