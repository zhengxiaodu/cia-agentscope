"""Renewable Redis lease. Configured Redis failures never grant ownership."""
import asyncio
import hashlib
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

COMPARE_DELETE = """-- lease_release
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
COMPARE_EXPIRE = """-- lease_renew
local owner = redis.call('get', KEYS[1])
if not owner then
  return -1
end
if owner ~= ARGV[1] then
  return -2
end
return redis.call('pexpire', KEYS[1], ARGV[2])
"""


class CoordinationUnavailable(RuntimeError):
    pass


class LeaseLost(CoordinationUnavailable):
    pass


def decode(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def transient_io_error(exc):
    """Redis has optional dependencies here; classify transport failures by MRO."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & {"AuthenticationError", "AuthorizationError", "PermissionError"}:
        return False
    return isinstance(exc, (OSError, TimeoutError)) or bool(names & {
        "TimeoutError", "ConnectionError", "BusyLoadingError",
    })


class RedisLease:
    def __init__(self, redis, key, *, ttl=120, wait_timeout=0,
                 poll_interval=0.25, on_lost=None, token=None):
        self.redis, self.key = redis, key
        self.ttl, self.wait_timeout = ttl, wait_timeout
        self.poll_interval, self.on_lost = poll_interval, on_lost
        self.io_timeout = max(0.01, min(5.0, ttl / 3))
        self.token = token or uuid.uuid4().hex
        self.lost = asyncio.Event()
        self._renew_task = None
        self._release_task = None
        self._releasing = False
        self._owned = False
        self._valid_until = 0.0
        self._renew_at = 0.0
        self._acquired_at = 0.0
        self._owner_tag = hashlib.sha256(self.token.encode()).hexdigest()[:12]
        self._instance = os.getenv("HOSTNAME", "unknown")
        self._safety_margin = min(1.0, ttl / 10)

    async def acquire(self):
        deadline = time.monotonic() + self.wait_timeout
        while True:
            sent_at = time.monotonic()
            try:
                acquired = await asyncio.wait_for(
                    self.redis.set(self.key, self.token, nx=True, px=max(1, int(self.ttl * 1000))),
                    timeout=self.io_timeout,
                )
            except Exception as exc:
                raise CoordinationUnavailable("Redis 协调服务不可用") from exc
            if acquired:
                self._owned = True
                self._acquired_at = sent_at
                # The server could execute SET immediately and its reply arrive late.
                self._valid_until = sent_at + self.ttl - self._safety_margin
                self._renew_at = sent_at + self.ttl / 3
                if time.monotonic() >= self._valid_until:
                    self._mark_lost()
                    await self.release()
                    raise LeaseLost("获取租约的响应已超出有效期，请重试")
                self._renew_task = asyncio.create_task(self._renew())
                if self.key.startswith("chat_run:"):
                    logger.info(
                        "Redis chat lease acquired key=%s instance=%s owner_tag=%s ttl_s=%s",
                        self.key, self._instance, self._owner_tag, self.ttl,
                    )
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))

    def _mark_lost(self):
        if self.lost.is_set():
            return
        logger.warning("Redis lease ownership lost key=%s", self.key)
        self.lost.set()
        if self.on_lost:
            self.on_lost()

    def locally_valid(self):
        """Validity backed by the last confirmed SET/renewal, never by retries."""
        return self._owned and not self.lost.is_set() and time.monotonic() < self._valid_until

    async def _renew(self):
        try:
            while not self._releasing:
                await asyncio.sleep(max(0, self._renew_at - time.monotonic()))
                if self._releasing:
                    return
                sent_at = time.monotonic()
                remaining = self._valid_until - sent_at
                if remaining <= 0:
                    logger.warning(
                        "Redis lease renew skipped: validity window expired "
                        "(event loop likely blocked), key=%s", self.key,
                    )
                    self._mark_lost()
                    return
                try:
                    renewed = await asyncio.wait_for(
                        self.redis.eval(COMPARE_EXPIRE, 1, self.key, self.token, max(1, int(self.ttl * 1000))),
                        timeout=min(self.io_timeout, remaining),
                    )
                except Exception as exc:
                    if not transient_io_error(exc) or time.monotonic() >= self._valid_until:
                        logger.warning(
                            "Redis lease renew failed (non-transient or past "
                            "validity), key=%s exc=%s", self.key, exc,
                        )
                        self._mark_lost()
                        return
                    # Retry promptly inside the OLD confirmed validity window.
                    # An ambiguous timeout never extends that window.
                    self._renew_at = min(self._valid_until, time.monotonic() + min(0.05, self.ttl / 100))
                    continue
                if renewed <= 0:
                    # 在同一个 Lua 操作内区分缺失与换主；0 保留为未知拒绝。
                    # NX 不会覆盖活跃锁，不能仅凭拒绝就认定另一副本抢锁。
                    reason = {-1: "key_missing", -2: "owner_mismatch"}.get(
                        renewed, "renew_rejected"
                    )
                    logger.warning(
                        "Redis lease renew rejected reason=%s key=%s instance=%s "
                        "owner_tag=%s lease_age_s=%.3f local_remaining_s=%.3f ttl_s=%s",
                        reason, self.key, self._instance, self._owner_tag,
                        sent_at - self._acquired_at, remaining, self.ttl,
                    )
                    self._mark_lost()
                    return
                self._valid_until = sent_at + self.ttl - self._safety_margin
                self._renew_at = sent_at + self.ttl / 3
                if time.monotonic() >= self._valid_until:
                    logger.warning(
                        "Redis lease expired right after renew "
                        "(event loop blocked during eval), key=%s", self.key,
                    )
                    self._mark_lost()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Redis lease renewal failed key=%s", self.key, exc_info=True)
            self._mark_lost()

    async def assert_owned(self):
        if not self._owned or self.lost.is_set() or time.monotonic() >= self._valid_until:
            self._mark_lost()
            raise LeaseLost("运行锁已失效，请重试")
        try:
            owner = decode(await asyncio.wait_for(self.redis.get(self.key), timeout=self.io_timeout))
        except Exception as exc:
            self._mark_lost()
            raise CoordinationUnavailable("Redis 协调服务不可用") from exc
        if owner != self.token or time.monotonic() >= self._valid_until:
            self._mark_lost()
            raise LeaseLost("运行锁已失效，请重试")

    async def release(self):
        if self._release_task is None:
            self._releasing = True
            self._release_task = asyncio.create_task(self._release())
        await asyncio.shield(self._release_task)

    async def _release(self):
        if self._renew_task is not None:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except asyncio.CancelledError:
                pass
            self._renew_task = None
        if self._owned:
            self._owned = False
            try:
                await asyncio.wait_for(self.redis.eval(COMPARE_DELETE, 1, self.key, self.token), timeout=self.io_timeout)
            except Exception:
                # TTL remains the final cleanup boundary; never delete someone else's lease.
                logger.warning("Redis lease release failed key=%s", self.key, exc_info=True)
