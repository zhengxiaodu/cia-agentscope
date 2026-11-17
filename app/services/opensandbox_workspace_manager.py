"""OpenSandbox 工作区管理器（唯一工作区后端）。

基于 OpenSandbox SDK 实现：
- 按 user_id 分配可复用沙箱
- 按 session_id 隔离工作目录
- TTL 淘汰 + 后台 sweeper 周期清扫

高可用增强：
- 创建沙箱失败时指数退避重试（最多 3 次）
- 沙箱崩溃自动检测与重建
- 连续失败降级标记（快速失败 + 告警日志）
- 结构化监控日志（创建延迟、成功率、活跃沙箱数）
"""
import asyncio
import inspect
import json
import logging
import os
import shlex
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.filesystem import WriteEntry

from agentscope.skill import Skill

from app.services.workspace_file_access import (
    build_list_skills_command,
    parse_skills_output,
)
from app.services.redis_lease import (
    COMPARE_DELETE,
    CoordinationUnavailable,
    LeaseLost,
    RedisLease,
)
from app.services.chat_runtime import current_chat_run

logger = logging.getLogger(__name__)

# ---- 高可用配置常量 ----
_MAX_CREATE_RETRIES = 3          # 创建沙箱最大重试次数
_RETRY_BASE_DELAY = 2.0          # 重试基础延迟（秒），指数退避
_DEGRADE_THRESHOLD = 5           # 连续失败 N 次后进入降级状态
_DEGRADE_RECOVERY_TIME = 60.0    # 降级后恢复探测间隔（秒）

# ---- 多副本分布式锁配置（Redis） ----
_DIST_LOCK_TTL = 120             # 新建沙箱分布式锁 TTL（秒）
_DIST_LOCK_WAIT_TIMEOUT = 30.0   # 等待分布式锁总超时（秒）
_DIST_LOCK_POLL_INTERVAL = 0.5   # 等锁轮询间隔（秒）
_PREPARE_RETRY_TIMES = 2
_PREPARE_RETRY_DELAY = 1.0
_MAP_TTL_SAFETY_MARGIN = 60

# Publish only while owning the creation lease, and never overwrite an existing
# mapping. A creator whose lease expired cannot expose its sandbox to other Pods.
_PUBLISH_MAP = """-- workspace_publish
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('exists', KEYS[2]) == 1 then return 0 end
redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""

_RETIRE_MAP = """-- workspace_retire
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('get', KEYS[2]) ~= ARGV[2] then return 0 end
return redis.call('del', KEYS[2])
"""

# Extend the map TTL only while it still points at the sandbox we just renewed,
# so a stale entry for another Pod's newer sandbox is never prolonged.
_RENEW_MAP = """-- workspace_renew_map
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('expire', KEYS[1], ARGV[2])
"""


class SandboxNearExpiry(RuntimeError):
    pass


class SessionDirectoryError(RuntimeError):
    pass


class _Entry:
    __slots__ = (
        "sandbox", "last_access", "user_id", "session_ids", "workdir", "skills_meta",
    )

    def __init__(self, sandbox: Sandbox, user_id: str, session_ids: set | None = None):
        self.sandbox = sandbox
        self.last_access = time.monotonic()
        self.user_id = user_id
        self.session_ids = session_ids or set()
        self.workdir = "/data/workspaces"
        # 技能元信息缓存：技能只在沙箱创建时注入，沙箱生命周期内不变，
        # None 表示尚未扫描。见 list_skills。
        self.skills_meta: list[dict] | None = None


class OpenSandboxWorkspaceManager:
    """OpenSandbox 工作区管理器，底层使用 OpenSandbox SDK。
    隔离策略：同一 user_id 复用同一沙箱，不同 user_id 各自独立沙箱；
    工作路径按 session_id 隔离（沙箱内 /data/workspaces/{session_id}）。

    高可用特性：
    - 创建失败指数退避重试
    - 沙箱崩溃自动检测与重建
    - 连续失败降级保护
    - 结构化监控日志
    """

    def __init__(
        self,
        connection_config: ConnectionConfig,
        base_image: str,
        basedir: str,
        ttl: float,
        resource: dict | None = None,
        ready_timeout: timedelta = timedelta(seconds=120),
        pool_size: int = 0,
        pool_refill: bool = True,
        redis_client=None,
    ):
        self._config = connection_config
        self._base_image = base_image
        self._basedir = basedir  # 沙箱内工作目录根路径
        self._ttl = ttl
        self._resource = resource or {"cpu": "100m", "memory": "128Mi"}
        self._ready_timeout = ready_timeout
        # 多副本：Redis 客户端（None 时退化为单进程内存映射行为）
        self._redis = redis_client
        self._cache: dict[str, _Entry] = {}
        self._deferred_clients: list[tuple[str, Sandbox]] = []
        self._closing = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._struct_lock = asyncio.Lock()
        self._sweeper_task: Optional[asyncio.Task] = None
        self._sweeper_stop = asyncio.Event()
        self._sweeper_interval = max(1.0, min(self._ttl / 3, 300.0))

        # ---- 高可用状态 ----
        self._consecutive_failures = 0
        self._degraded = False
        self._degrade_since: float = 0.0
        # ---- 监控计数 ----
        self._total_created = 0
        self._total_failed = 0
        # ---- 预热池 ----
        self._pool_size = pool_size
        self._pool_refill = pool_refill
        self._warm_pool: list[Sandbox] = []
        self._pool_lock = asyncio.Lock()

    @staticmethod
    def _workspace_id(user_id: str) -> str:
        return f"user-{user_id}"

    def _session_dir(self, session_id: str) -> str:
        return f"{self._basedir}/{session_id}"

    async def _get_lock(self, wid: str) -> asyncio.Lock:
        async with self._struct_lock:
            lock = self._locks.get(wid)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[wid] = lock
            return lock

    # ---- 多副本：Redis 用户→沙箱映射 ----
    # Redis 已配置时必须完成协调；读取失败不能被误认为“没有沙箱”。
    async def _redis_get_map(self, wid: str) -> Optional[str]:
        """读取映射；仅确实无映射时返回 None，Redis 故障时快速失败。"""
        if self._redis is None:
            return None
        try:
            value = await asyncio.wait_for(self._redis.get(f"sandbox_map:{wid}"), timeout=5)
            if value is None:
                return None
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return str(value)
        except Exception as e:
            raise CoordinationUnavailable("Redis 沙箱映射暂不可用，请稍后重试") from e

    async def _redis_publish_map(self, wid: str, sandbox_id: str, lease: RedisLease, expires_at=None) -> None:
        """在持有创建租约的同一个 Redis 原子操作内发布沙箱。"""
        map_ttl = self._remaining_map_ttl(expires_at)
        try:
            published = await asyncio.wait_for(self._redis.eval(
                _PUBLISH_MAP, 2, f"sandbox_lock:{wid}", f"sandbox_map:{wid}",
                lease.token, sandbox_id, map_ttl,
            ), timeout=lease.io_timeout)
        except Exception as e:
            raise CoordinationUnavailable("Redis 沙箱映射发布失败") from e
        if not published:
            raise LeaseLost("沙箱创建租约已失效或映射已更新，请重试")

    async def _redis_del_map(self, wid: str, sandbox_id: str) -> bool:
        """只删除已经确认消失的同一个沙箱，保护其他 Pod 的新映射。"""
        if self._redis is None:
            return False
        try:
            return bool(await asyncio.wait_for(self._redis.eval(
                COMPARE_DELETE, 1, f"sandbox_map:{wid}", sandbox_id,
            ), timeout=5))
        except Exception as e:
            raise CoordinationUnavailable("Redis 沙箱映射清理失败") from e

    async def _renew_expiration(self, sbx: Sandbox, wid: str) -> None:
        """滑动续期：把沙箱到期时间推到"当前时间 + TTL"。

        沙箱创建时带的是绝对 TTL（Sandbox.create(timeout=...)），长任务
        （如 PPTX 生成，工具调用多、耗时长）可能在中途被服务端回收，
        导致后续所有命令 404 SANDBOX_NOT_FOUND。活跃会话每轮续期后，
        只有真正空闲满 TTL 的沙箱才会被回收，与"淘汰空闲沙箱"的意图一致
        （SDK 明确 renew 不受创建时上限约束）。

        Redis 已配置时同步刷新映射 TTL：映射 TTL 原按创建时剩余有效期设定，
        若只续期服务端而不刷新映射，映射会先过期，其他 Pod 会误判沙箱消失
        而另建新沙箱。

        全部失败均降级为告警，不影响本轮对话（沙箱可能仍存活，仅未延长）。
        """
        try:
            resp = await asyncio.wait_for(
                sbx.renew(timedelta(seconds=int(self._ttl))), timeout=5
            )
        except Exception:
            logger.warning(
                f"[opensandbox_ws] 沙箱续期失败 wid={wid} sandbox_id={sbx.id}",
                exc_info=True,
            )
            return
        if self._redis is None:
            return
        try:
            map_ttl = self._remaining_map_ttl(getattr(resp, "expires_at", None))
            await asyncio.wait_for(self._redis.eval(
                _RENEW_MAP, 1, f"sandbox_map:{wid}", sbx.id, map_ttl,
            ), timeout=5)
        except Exception:
            logger.warning(
                f"[opensandbox_ws] 沙箱映射 TTL 刷新失败 wid={wid} "
                f"sandbox_id={sbx.id}",
                exc_info=True,
            )

    # ---- 高可用：降级检查 ----
    def _check_degraded(self) -> bool:
        """检查是否处于降级状态，若恢复时间已到则尝试恢复。"""
        if not self._degraded:
            return False
        elapsed = time.monotonic() - self._degrade_since
        if elapsed >= _DEGRADE_RECOVERY_TIME:
            logger.info("[opensandbox_ws] 降级恢复探测：尝试退出降级状态")
            self._degraded = False
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self) -> None:
        """记录成功，重置连续失败计数。"""
        self._consecutive_failures = 0
        self._total_created += 1

    def _record_failure(self) -> None:
        """记录失败，检查是否触发降级。"""
        self._consecutive_failures += 1
        self._total_failed += 1
        if self._consecutive_failures >= _DEGRADE_THRESHOLD and not self._degraded:
            self._degraded = True
            self._degrade_since = time.monotonic()
            logger.error(
                f"[opensandbox_ws] 连续 {self._consecutive_failures} 次创建失败，"
                f"进入降级状态（{_DEGRADE_RECOVERY_TIME}s 后自动恢复探测）"
            )

    # ---- 预热池管理 ----
    async def _warm_up(self) -> None:
        """启动时预热：提前创建 pool_size 个空闲沙箱。"""
        if self._pool_size <= 0:
            return
        logger.info(f"[opensandbox_ws] 预热池启动，目标 size={self._pool_size}")
        for i in range(self._pool_size):
            try:
                sbx = await self._create_sandbox_raw()
                async with self._pool_lock:
                    self._warm_pool.append(sbx)
                logger.info(
                    f"[opensandbox_ws] 预热池补充 {i+1}/{self._pool_size} "
                    f"sandbox_id={sbx.id}"
                )
            except Exception as e:
                logger.warning(f"[opensandbox_ws] 预热池创建失败 {i+1}/{self._pool_size}: {e}")
        logger.info(f"[opensandbox_ws] 预热池就绪 size={len(self._warm_pool)}")

    async def _acquire_from_pool(self) -> Sandbox | None:
        """从预热池获取一个可用沙箱，池空或沙箱已死则返回 None。"""
        async with self._pool_lock:
            while self._warm_pool:
                sbx = self._warm_pool.pop()
                if await self._is_sandbox_alive(sbx):
                    if self._redis is not None:
                        try:
                            info = await asyncio.wait_for(sbx.get_info(), timeout=5)
                            self._remaining_map_ttl(info.expires_at)
                        except Exception as exc:
                            try:
                                await sbx.destroy()  # Unassigned pool entry has no users.
                            except Exception:
                                logger.warning("[opensandbox_ws] 回收不可分配的预热沙箱失败", exc_info=True)
                            finally:
                                await self._close_client(sbx)
                            if isinstance(exc, SandboxNearExpiry):
                                continue
                            raise
                    logger.info(
                        f"[opensandbox_ws] 从预热池分配 sandbox_id={sbx.id} "
                        f"remaining={len(self._warm_pool)}"
                    )
                    # 异步补充（不阻塞当前请求）
                    if self._pool_refill:
                        asyncio.create_task(self._refill_one())
                    return sbx
                # 池中沙箱已死，销毁
                try:
                    await sbx.destroy()
                except Exception:
                    pass
        return None

    async def _refill_one(self) -> None:
        """后台补充一个沙箱到预热池。"""
        try:
            sbx = await self._create_sandbox_raw()
            async with self._pool_lock:
                if len(self._warm_pool) < self._pool_size:
                    self._warm_pool.append(sbx)
                    logger.info(
                        f"[opensandbox_ws] 预热池补充 sandbox_id={sbx.id} "
                        f"size={len(self._warm_pool)}/{self._pool_size}"
                    )
                else:
                    await sbx.destroy()
        except Exception as e:
            logger.warning(f"[opensandbox_ws] 预热池补充失败: {e}")

    async def _destroy_pool(self) -> None:
        """销毁预热池中所有空闲沙箱。"""
        async with self._pool_lock:
            for sbx in self._warm_pool:
                try:
                    await sbx.destroy()
                except Exception:
                    pass
            count = len(self._warm_pool)
            self._warm_pool.clear()
        if count:
            logger.info(f"[opensandbox_ws] 预热池已清空 destroyed={count}")

    # ---- 高可用：带重试的沙箱创建 ----
    async def _create_sandbox_with_retry(self) -> Sandbox:
        """创建沙箱：优先从预热池取，池空则新建（失败时指数退避重试）。"""
        # 优先从预热池获取
        pooled = await self._acquire_from_pool()
        if pooled is not None:
            self._record_success()
            return pooled
        # 池空：后台回填一个，同时同步新建不阻塞当前请求
        if self._pool_refill and self._pool_size > 0:
            asyncio.create_task(self._refill_one())
        return await self._create_sandbox_raw()

    async def _create_sandbox_raw(self) -> Sandbox:
        """底层沙箱创建（带指数退避重试）。"""
        last_exc = None
        for attempt in range(1, _MAX_CREATE_RETRIES + 1):
            try:
                t0 = time.monotonic()
                sbx = await Sandbox.create(
                    self._base_image,
                    connection_config=self._config,
                    timeout=timedelta(seconds=int(self._ttl)),
                    resource=self._resource,
                    ready_timeout=self._ready_timeout,
                )
                latency_ms = (time.monotonic() - t0) * 1000
                logger.info(
                    f"[opensandbox_ws] 沙箱创建成功 sandbox_id={sbx.id} "
                    f"latency={latency_ms:.0f}ms attempt={attempt}"
                )
                self._record_success()
                return sbx
            except Exception as e:
                last_exc = e
                self._record_failure()
                if attempt < _MAX_CREATE_RETRIES:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        f"[opensandbox_ws] 沙箱创建失败 attempt={attempt}/{_MAX_CREATE_RETRIES} "
                        f"error={e} retry_in={delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        f"[opensandbox_ws] 沙箱创建最终失败 attempts={_MAX_CREATE_RETRIES} "
                        f"error={e}"
                    )
        raise last_exc  # type: ignore

    # ---- 高可用：沙箱存活探测 ----
    async def _is_sandbox_alive(self, sbx: Sandbox) -> bool:
        """探测沙箱是否仍然存活。"""
        try:
            result = await sbx.commands.run("echo 1")
            return result.exit_code == 0
        except Exception:
            return False

    async def _touch_session_dir(self, sbx: Sandbox, session_id: str) -> bool:
        """建会话目录并顺带探活：一条命令同时完成 mkdir 与存活判断。

        exit_code == 0 → 沙箱存活且目录就绪；非 0 或抛异常 → 视为沙箱已死。
        把两次往返压成一次，是每轮复用路径的主要开销来源。
        """
        session_dir = self._session_dir(session_id)
        try:
            result = await sbx.commands.run(f"mkdir -p {shlex.quote(session_dir)} && echo 1")
            return result.exit_code == 0
        except Exception:
            return False

    async def _evict_locked(self, wid: str) -> None:
        entry = self._cache.pop(wid, None)
        if entry is not None:
            if self._redis is not None:
                # 本地 TTL/退出仅释放当前 Pod 的 SDK 连接。其他 Pod 仍可能
                # 使用该沙箱；共享沙箱的最终销毁由 server TTL 负责。
                if not self._closing:
                    try:
                        active = await asyncio.wait_for(self._other_active_runs(wid, ""), timeout=5)
                    except Exception:
                        active = True  # Unknown usage must not close an in-flight SDK client.
                    if active:
                        self._deferred_clients.append((wid, entry.sandbox))
                        return
                await self._close_client(entry.sandbox)
                return
            try:
                await entry.sandbox.destroy()
            except Exception as e:
                # 幂等：远端沙箱已不存在（server 侧 TTL 回收/运维清理）
                # 视为销毁成功，不刷 ERROR 堆栈
                msg = str(e)
                if "SANDBOX_NOT_FOUND" in msg or "not found" in msg.lower():
                    logger.info(
                        f"[opensandbox_ws] 远端沙箱已不存在，视为已销毁 wid={wid}"
                    )
                else:
                    logger.exception(f"[opensandbox_ws] 销毁沙箱失败 wid={wid}")
            logger.info(f"[opensandbox_ws] 淘汰工作区 wid={wid}")

    @staticmethod
    async def _close_client(sbx: Sandbox) -> None:
        """释放 SDK 客户端资源，不触发远端 destroy。"""
        close = getattr(sbx, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("[opensandbox_ws] 关闭 SDK 连接失败", exc_info=True)

    async def _drain_deferred_clients(self, force=False):
        pending, self._deferred_clients = self._deferred_clients, []
        for wid, sbx in pending:
            try:
                active = not force and await asyncio.wait_for(self._other_active_runs(wid, ""), timeout=5)
            except Exception:
                active = True
            if active:
                self._deferred_clients.append((wid, sbx))
            else:
                await self._close_client(sbx)

    @staticmethod
    def _sandbox_gone(exc: Exception) -> bool:
        # 连接超时、connection reset 和普通非零退出均不能证明沙箱消失。
        return ("SANDBOX_NOT_FOUND" in str(exc)
                or getattr(exc, "code", None) == "SANDBOX_NOT_FOUND"
                or getattr(getattr(exc, "error", None), "code", None) == "SANDBOX_NOT_FOUND")

    async def _prepare_shared_session(self, sbx: Sandbox, session_id: str) -> None:
        """保留 SDK 异常类型，区分明确不存在和临时不可达。"""
        result = await sbx.commands.run(
            f"mkdir -p {shlex.quote(self._session_dir(session_id))} && echo 1"
        )
        if result.exit_code != 0:
            raise SessionDirectoryError("已有沙箱会话目录暂不可用，请稍后重试")

    def _remaining_map_ttl(self, expires_at) -> int:
        if expires_at is None or expires_at.tzinfo is None:
            raise CoordinationUnavailable("无法确认沙箱实际到期时间")
        margin = min(_MAP_TTL_SAFETY_MARGIN, self._ttl / 10)
        remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds() - margin)
        if remaining < 1:
            raise SandboxNearExpiry("沙箱剩余有效期不足，请重新创建")
        return remaining

    @staticmethod
    def _prepare_retryable(exc: Exception) -> bool:
        code = getattr(getattr(exc, "error", None), "code", None)
        return isinstance(exc, (SessionDirectoryError, ConnectionError, TimeoutError)) or code in {
            "UNHEALTHY", "CONNECTION", "TIMEOUT", "RATE_LIMIT",
        }

    async def _prepare_with_retry(self, sbx: Sandbox, session_id: str) -> None:
        for attempt in range(_PREPARE_RETRY_TIMES + 1):
            try:
                await self._prepare_shared_session(sbx, session_id)
                return
            except Exception as exc:
                if self._sandbox_gone(exc) or not self._prepare_retryable(exc) or attempt == _PREPARE_RETRY_TIMES:
                    raise
                await asyncio.sleep(_PREPARE_RETRY_DELAY)

    async def _other_active_runs(self, wid: str, session_id: str) -> bool:
        own = current_chat_run.get()
        async for key in self._redis.scan_iter(match="chat_run:*"):
            raw = await self._redis.get(key)
            if raw is None:
                continue
            token = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            state = json.loads(token)
            if not isinstance(state, dict) or "user_id" not in state or "run_id" not in state:
                raise CoordinationUnavailable("会话运行记录异常，不能安全切换沙箱")
            if self._workspace_id(str(state["user_id"])) != wid:
                continue
            name = key.decode("utf-8") if isinstance(key, bytes) else key
            if own == (session_id, token) and name == f"chat_run:{session_id}":
                continue
            return True
        return False

    async def _global_fault_confirmed(self, sbx: Sandbox) -> bool:
        try:
            info = await asyncio.wait_for(sbx.get_info(), timeout=5)
            if info.status.state in {"Failed", "Terminated", "Error"}:
                return True
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(sbx.commands.run("echo 1"), timeout=5)
                    if result.exit_code == 0:
                        return False  # One directory is broken; the sandbox is usable.
                except Exception as exc:
                    if getattr(getattr(exc, "error", None), "code", None) != "UNHEALTHY":
                        return False  # Transport/auth errors cannot prove sandbox damage.
                if attempt == 0:
                    await asyncio.sleep(_PREPARE_RETRY_DELAY)
            return True
        except Exception:
            return False

    async def _retire_shared_sandbox(self, wid, sandbox_id, session_id, sbx=None, lease=None):
        """Quiescent, proven-bad generation only; preserve old data until server TTL."""
        own_lease = lease is None
        connected = None
        if own_lease:
            lease = RedisLease(self._redis, f"sandbox_lock:{wid}", wait_timeout=5)
        try:
            if own_lease and not await lease.acquire():
                return False
            await lease.assert_owned()
            if await self._redis_get_map(wid) != sandbox_id:
                return True
            try:
                if await asyncio.wait_for(self._other_active_runs(wid, session_id), timeout=5):
                    return False
            except Exception as exc:
                raise CoordinationUnavailable("无法确认沙箱是否仍被其他会话使用") from exc
            if sbx is None:
                try:
                    connected = await Sandbox.connect(sandbox_id, connection_config=self._config, skip_health_check=True)
                    sbx = connected
                except Exception:
                    return False
            if not await self._global_fault_confirmed(sbx):
                return False
            await lease.assert_owned()
            try:
                retired = await asyncio.wait_for(self._redis.eval(
                    _RETIRE_MAP, 2, f"sandbox_lock:{wid}", f"sandbox_map:{wid}",
                    lease.token, sandbox_id,
                ), timeout=lease.io_timeout)
            except Exception as exc:
                raise CoordinationUnavailable("沙箱切换失败") from exc
            if not retired:
                await lease.assert_owned()
                return await self._redis_get_map(wid) != sandbox_id
            logger.warning("[opensandbox_ws] 已停止分配故障沙箱 wid=%s sandbox_id=%s；由服务端 TTL 清理", wid, sandbox_id)
            return True
        finally:
            if connected is not None:
                await self._close_client(connected)
            if own_lease:
                await lease.release()

    async def _evict(self, wid: str) -> None:
        lock = await self._get_lock(wid)
        async with lock:
            await self._evict_locked(wid)

    async def _reuse_local_entry(self, wid: str, session_id: str) -> Optional[Sandbox]:
        """复用本地连接；共享模式每次以 Redis 映射为准，不回写旧 ID。"""
        entry = self._cache.get(wid)
        if entry is None:
            return None
        if (time.monotonic() - entry.last_access) > self._ttl:
            await self._evict_locked(wid)
            return None
        if self._redis is not None:
            if await self._redis_get_map(wid) != entry.sandbox.id:
                await self._evict_locked(wid)
                return None
            try:
                await self._prepare_with_retry(entry.sandbox, session_id)
            except Exception as exc:
                if self._sandbox_gone(exc):
                    await self._evict_locked(wid)
                    await self._redis_del_map(wid, entry.sandbox.id)
                    return None
                if self._prepare_retryable(exc) and await self._retire_shared_sandbox(wid, entry.sandbox.id, session_id, sbx=entry.sandbox):
                    await self._evict_locked(wid)
                    return None
                raise
            if await self._redis_get_map(wid) != entry.sandbox.id:
                await self._evict_locked(wid)
                return None
        # 高可用：探测沙箱存活
        elif not await self._touch_session_dir(entry.sandbox, session_id):
            logger.warning(f"[opensandbox_ws] 沙箱已崩溃，重建 wid={wid}")
            await self._evict_locked(wid)
            return None
        entry.last_access = time.monotonic()
        session_dir = self._session_dir(session_id)
        entry.session_ids.add(session_id)
        entry.workdir = session_dir
        # 在 Sandbox 对象上设置 workdir，兼容 AgentRegistry 的 system_prompt 构建
        entry.sandbox.workdir = session_dir
        # 滑动续期：本轮已确认沙箱存活，延长到期时间，避免长任务进行中
        # （如 PPTX 生成）沙箱被创建时的固定 TTL 回收
        await self._renew_expiration(entry.sandbox, wid)
        logger.info(f"[opensandbox_ws] 复用沙箱 wid={wid} session={session_id}")
        return entry.sandbox

    async def _connect_from_redis(
        self, wid: str, user_id: str, session_id: str, lease=None
    ) -> Optional[Sandbox]:
        """连接共享沙箱；只有明确不存在时才比较删除映射。"""
        sandbox_id = await self._redis_get_map(wid)
        if not sandbox_id:
            return None
        sbx = None
        try:
            sbx = await Sandbox.connect(sandbox_id, connection_config=self._config)
            await self._prepare_with_retry(sbx, session_id)
            if await self._redis_get_map(wid) != sandbox_id:
                await self._close_client(sbx)
                return None
        except Exception as e:
            try:
                if self._sandbox_gone(e):
                    await self._redis_del_map(wid, sandbox_id)
                    return None
                if self._prepare_retryable(e) and await self._retire_shared_sandbox(wid, sandbox_id, session_id, sbx=sbx, lease=lease):
                    return None
                raise
            finally:
                if sbx is not None:
                    await self._close_client(sbx)
        session_dir = self._session_dir(session_id)
        new_entry = _Entry(sbx, user_id, session_ids={session_id})
        new_entry.workdir = session_dir
        # 在 Sandbox 对象上设置 workdir，兼容 AgentRegistry 的 system_prompt 构建
        sbx.workdir = session_dir
        # 滑动续期：跨 Pod 复用前已确认沙箱存活，延长到期时间
        await self._renew_expiration(sbx, wid)
        self._cache[wid] = new_entry
        logger.info(
            f"[opensandbox_ws] 跨 Pod 复用沙箱 wid={wid} sandbox_id={sandbox_id} "
            f"session={session_id}"
        )
        return sbx

    async def create_workspace(
        self, user_id: str, session_id: str, skill_dirs: list[str] | None = None,
        langfuse_service=None,
    ) -> Sandbox:
        """创建或复用沙箱工作区。"""
        # 降级检查
        if self._check_degraded():
            logger.warning(
                f"[opensandbox_ws] 降级状态，拒绝创建 user={user_id} "
                f"(active_sandboxes={len(self._cache)})"
            )
            raise RuntimeError(
                "OpenSandbox 工作区管理器处于降级状态，请稍后重试"
            )

        wid = self._workspace_id(user_id)
        lock = await self._get_lock(wid)
        async with lock:
            # 1. 复用本地已有沙箱
            sbx = await self._reuse_local_entry(wid, session_id)
            if sbx is not None:
                return sbx

            # 2. 多副本：本地未命中，查 Redis 映射跨 Pod 复用
            sbx = await self._connect_from_redis(wid, user_id, session_id)
            if sbx is not None:
                return sbx

            # 3. Redis 已配置时必须持有可续租的创建锁，覆盖慢创建及所有重试。
            lease = None
            sbx = None
            published = False
            publish_attempted = False
            try:
                if self._redis is not None:
                    lease = RedisLease(
                        self._redis, f"sandbox_lock:{wid}", ttl=_DIST_LOCK_TTL,
                        wait_timeout=_DIST_LOCK_WAIT_TIMEOUT,
                        poll_interval=_DIST_LOCK_POLL_INTERVAL,
                    )
                    if not await lease.acquire():
                        # 创建者可能已发布但尚未释放锁，允许复用该映射。
                        reused = await self._connect_from_redis(
                            wid, user_id, session_id
                        )
                        if reused is not None:
                            return reused
                        raise CoordinationUnavailable("沙箱正在初始化，请稍后重试")
                    await lease.assert_owned()
                    # 拿锁后复查，避免等待期间其他 Pod 已经建好沙箱。
                    reused = await self._connect_from_redis(wid, user_id, session_id, lease=lease)
                    if reused is not None:
                        return reused
                    if await asyncio.wait_for(self._other_active_runs(wid, session_id), timeout=5):
                        raise CoordinationUnavailable("工作区映射缺失，但其他会话仍在运行，请稍后重试")

                # 创建新沙箱（带重试）
                if langfuse_service:
                    with langfuse_service.start_span(
                        "workspace-initialize",
                        input={"user_id": user_id, "session_id": session_id},
                    ) as init_span:
                        sbx = await self._create_sandbox_with_retry()
                        if init_span:
                            try:
                                init_span.update(output={"sandbox_id": sbx.id})
                            except Exception:
                                pass
                else:
                    sbx = await self._create_sandbox_with_retry()
                session_dir = self._session_dir(session_id)
                if lease is not None:
                    await lease.assert_owned()
                    await self._prepare_shared_session(sbx, session_id)
                elif not await self._touch_session_dir(sbx, session_id):
                    logger.warning(
                        f"[opensandbox_ws] 新建沙箱后会话目录准备失败 sandbox_id={sbx.id}"
                    )

                # 注入技能文件（将宿主技能目录内容写入沙箱）
                if skill_dirs:
                    await self._inject_skills(sbx, skill_dirs)

                if lease is not None:
                    await lease.assert_owned()
                    info = await asyncio.wait_for(sbx.get_info(), timeout=5)
                    publish_attempted = True
                    await self._redis_publish_map(wid, sbx.id, lease, expires_at=info.expires_at)
                published = True
                new_entry = _Entry(sbx, user_id, session_ids={session_id})
                new_entry.workdir = session_dir
                sbx.workdir = session_dir
                self._cache[wid] = new_entry
                logger.info(
                    f"[opensandbox_ws] 创建沙箱 wid={wid} sandbox_id={sbx.id} "
                    f"active_sandboxes={len(self._cache)}"
                )
                return sbx
            except BaseException:
                if sbx is not None and publish_attempted and not published:
                    # EVAL may have succeeded even if its response was lost. A
                    # visible (or unverifiable) mapping may already have readers.
                    try:
                        published = await self._redis_get_map(wid) == sbx.id
                    except CoordinationUnavailable:
                        published = True  # Server TTL is safe; destroying may not be.
                if sbx is not None and not published:
                    # 未发布的实例没有共享使用者，即使租约丢失也应回收。
                    try:
                        await sbx.destroy()
                    except Exception:
                        logger.warning("[opensandbox_ws] 回收未发布沙箱失败", exc_info=True)
                    else:
                        # 发布返回异常可能发生在 Redis 已写入之后。
                        # 仅在确认销毁后比较删除自己的 ID。
                        try:
                            await self._redis_del_map(wid, sbx.id)
                        except CoordinationUnavailable:
                            logger.warning("[opensandbox_ws] 未发布沙箱映射待 TTL 回收")
                    await self._close_client(sbx)
                elif sbx is not None and published:
                    await self._close_client(sbx)
                raise
            finally:
                if lease is not None:
                    await lease.release()

    @staticmethod
    def _collect_skill_entries(skill_dirs: list[str]) -> list[WriteEntry]:
        """读取宿主文件，在工作线程中执行以免阻塞租约续期和 SSE。"""
        entries: list[WriteEntry] = []
        for d in skill_dirs:
            if not d or not os.path.isdir(d):
                logger.warning(f"[opensandbox_ws] 技能目录不存在，跳过: {d}")
                continue
            skill_name = os.path.basename(d)
            for root, _, files in os.walk(d):
                for fname in files:
                    host_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(host_path, d)
                    with open(host_path, "rb") as f:
                        entries.append(WriteEntry(
                            path=f"/workspace/skills/{skill_name}/{rel_path}",
                            data=f.read(),
                            mode=644,
                        ))
        return entries

    async def _inject_skills(self, sbx: Sandbox, skill_dirs: list[str]) -> None:
        """将宿主技能目录内容写入沙箱 /workspace/skills/ 目录。"""
        entries = await asyncio.to_thread(self._collect_skill_entries, skill_dirs)
        if not entries:
            return
        try:
            await sbx.files.write_files(entries)
        except Exception:
            logger.warning(f"[opensandbox_ws] 注入技能文件失败 files={len(entries)}")

    async def get_workspace(
        self, user_id: str, session_id: str
    ) -> Optional[Sandbox]:
        """获取已有沙箱；共享模式过期只回收本地连接，再查全局映射。"""
        wid = self._workspace_id(user_id)
        lock = await self._get_lock(wid)
        async with lock:
            reused = await self._reuse_local_entry(wid, session_id)
            if reused is not None:
                return reused
            return await self._connect_from_redis(wid, user_id, session_id)

    # ---- 兼容 agentscope workspace 接口 ----
    def _entry_for(self, user_id: str) -> Optional["_Entry"]:
        """按 user_id 查缓存中的沙箱 entry，无则返回 None（不回落到其他用户）。"""
        wid = self._workspace_id(user_id) if user_id else None
        return self._cache.get(wid) if wid else None

    @staticmethod
    def _stdout(result) -> str:
        """把 SDK 命令结果的 Message 列表还原为多行文本。

        注意：SDK 的 logs.stdout 是 Message 列表，每条 m.text 是一行
        （本身不含换行符），按换行拼接后才能按行解析。
        """
        return "\n".join(m.text for m in result.logs.stdout)

    @staticmethod
    def _skill_from_meta(meta: dict) -> Skill:
        """dict 元信息 → agentscope Skill 对象（Toolkit 契约）。"""
        return Skill(
            name=meta["name"],
            description=meta["description"],
            dir=meta["directory"],
            # markdown 暂不读取完整内容，skill_instruction_template
            # 只用 name/description/dir；如需完整内容可后续按需加载
            markdown="",
            updated_at=0.0,
        )

    async def list_skills(self, user_id: str = "", session_id: str = "") -> list[Skill]:
        """列出沙箱内已注入的技能元数据（单次往返 + 按沙箱缓存）。

        一条 shell 命令扫完 /workspace/skills/ 并输出 `名称<TAB>描述首行`，
        结果缓存在 _Entry.skills_meta：技能只在沙箱创建时注入，沙箱生命周期内不变。

        无该用户的沙箱时返回 []——不得回落到其他用户的沙箱（技能清单跨用户泄漏）。

        返回 agentscope Skill 对象列表，与 agentscope workspace.list_skills() 接口一致
        （Toolkit.skills_or_loaders 仅接受 str | Skill | SkillLoaderBase，不接受 dict）。

        session_id 保留在签名中仅为调用方兼容，本方法不使用。
        """
        entry = self._entry_for(user_id)
        if entry is None:
            return []

        if entry.skills_meta is not None:
            return [self._skill_from_meta(m) for m in entry.skills_meta]

        try:
            result = await entry.sandbox.commands.run(build_list_skills_command())
            skills_meta = parse_skills_output(self._stdout(result))
        except Exception:
            logger.exception("[opensandbox_ws] list_skills 失败")
            return []

        entry.skills_meta = skills_meta
        return [self._skill_from_meta(m) for m in skills_meta]

    async def list_tools(self, user_id: str = "") -> list[str]:
        """返回沙箱内可用工具列表（兼容接口）。"""
        return ["bash", "read", "write", "edit", "glob", "grep"]

    @property
    def workdir(self) -> str:
        """兼容 agentscope workspace.workdir 属性。"""
        if self._cache:
            entry = next(iter(self._cache.values()))
            return entry.workdir
        return self._basedir

    # ---- 会话文件访问（复用 OpenSandboxToolAdapter）----
    async def _get_adapter(self, user_id: str, session_id: str):
        """获取会话对应的 OpenSandboxToolAdapter（纯读取，不触发创建）。

        通过 get_workspace 查询已有沙箱；沙箱不存在/已过期/已崩溃时
        返回 None，由调用方决定返回空集合或 None。沙箱的唯一创建入口
        在 orchestrator_service.run() 内的 get_workspace → create_workspace，
        避免文件快照等只读操作产生"提前创建沙箱"的副作用。
        """
        sbx = await self.get_workspace(user_id, session_id)
        if sbx is None:
            return None
        from app.services.opensandbox_adapter import OpenSandboxToolAdapter
        return OpenSandboxToolAdapter(sbx, workdir=self._session_dir(session_id))

    # 文本类后缀（小写，无点）
    _TEXT_EXTS = {
        "md", "markdown", "txt", "text", "json", "csv", "log",
        "py", "js", "ts", "jsx", "tsx", "java", "c", "cc", "cpp", "h", "hpp",
        "go", "rs", "rb", "php", "sh", "bash", "yml", "yaml", "xml", "html",
        "htm", "css", "sql", "ini", "conf", "toml", "env",
    }

    @staticmethod
    def _is_text_rel(rel_path: str) -> bool:
        ext = os.path.splitext(rel_path)[1].lstrip(".").lower()
        return ext in OpenSandboxWorkspaceManager._TEXT_EXTS

    @staticmethod
    def _normalize_rel(rel_path: str) -> str | None:
        """规范化相对路径，禁止 .. 和绝对路径；返回 POSIX 风格相对路径或 None（非法）。"""
        if not rel_path:
            return None
        if os.path.isabs(rel_path):
            return None
        rel = rel_path.replace("\\", "/").lstrip("/")
        if rel.startswith("..") or "/.." in rel or rel == "..":
            return None
        return rel

    async def list_session_files(
        self, user_id: str, session_id: str
    ) -> dict[str, tuple[str, str]]:
        """列出会话工作目录下所有文件及其修改签名。

        返回 {相对路径: (mtime, size)}（POSIX /），供快照差分识别
        同路径文件的内容变化（重生成/覆盖场景）。
        跳过顶层 data/skills 目录和 .mcp 文件；沙箱不存在/目录为空/异常返回空 dict。
        """
        try:
            adapter = await self._get_adapter(user_id, session_id)
            if adapter is None:
                return {}
            session_dir = self._session_dir(session_id)
            result = await adapter.bash(
                f"cd {session_dir} && "
                "find . -type f -printf '%T@ %s %p\\n' 2>/dev/null || true"
            )
            stdout = result.get("stdout", "") if isinstance(result, dict) else ""
            files: dict[str, tuple[str, str]] = {}
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                # 格式 "mtime size ./path"（路径可含空格，只切前两段）
                parts = line.split(maxsplit=2)
                if len(parts) == 3:
                    mtime, size, raw = parts
                    sig = (mtime, size)
                else:
                    # 非预期输出：按纯路径兜底，签名为 None（视为未变更）
                    raw, sig = line, None
                # 去掉 ./ 前缀
                rel = raw[2:] if raw.startswith("./") else raw
                rel = rel.lstrip("/")
                if not rel:
                    continue
                # 跳过顶层 data/skills 目录
                top = rel.split("/", 1)[0]
                if top in ("data", "skills"):
                    continue
                # 跳过 .mcp 文件
                if rel == ".mcp" or rel.endswith("/.mcp"):
                    continue
                files[rel] = sig
            return files
        except Exception:
            logger.warning(
                f"[opensandbox_ws] list_session_files 失败 "
                f"user={user_id} session={session_id}",
                exc_info=True,
            )
            return {}

    async def read_session_file(
        self, user_id: str, session_id: str, rel_path: str
    ) -> bytes | None:
        """读取会话文件字节。文本走 adapter.read，二进制走 base64 解码。

        文件不存在返回 None；rel_path 非法（含 .. / 绝对路径）返回 None。
        """
        rel = self._normalize_rel(rel_path)
        if rel is None:
            return None
        try:
            adapter = await self._get_adapter(user_id, session_id)
            if adapter is None:
                return None
            abs_path = f"{self._session_dir(session_id)}/{rel}"
            if self._is_text_rel(rel):
                text = await adapter.read(abs_path)
                return text.encode("utf-8") if isinstance(text, str) else bytes(text)
            # 二进制：优先走 execd 文件接口读原始字节。
            # 原 base64 shell 通道把文件内容塞进命令 stdout，受沙箱
            # 命令输出上限截断，大文件（如高分 png）读失败导致下载 404；
            # 文件接口按字节传输无此限制。
            try:
                return await adapter.read_bytes(abs_path)
            except Exception:
                logger.warning(
                    f"[opensandbox_ws] read_bytes 失败，回退 base64 "
                    f"user={user_id} session={session_id} rel={rel_path}",
                    exc_info=True,
                )
            # 回退：base64 shell 通道（路径做 shell 安全引用，防空格/特殊字符）
            import shlex
            result = await adapter.bash(
                f"base64 -w0 {shlex.quote(abs_path)} 2>/dev/null || true"
            )
            stdout = result.get("stdout", "") if isinstance(result, dict) else ""
            stdout = stdout.strip()
            if not stdout:
                return None
            import base64
            return base64.b64decode(stdout)
        except Exception:
            logger.warning(
                f"[opensandbox_ws] read_session_file 失败 "
                f"user={user_id} session={session_id} rel={rel_path}",
                exc_info=True,
            )
            return None

    async def write_session_file(
        self, user_id: str, session_id: str, rel_path: str, content: str
    ) -> bool:
        """把文本写入会话工作目录（上传文件解析内容落沙箱使用）。

        rel_path 需为 uploads/ 下的相对路径；失败返回 False，
        调用方静默降级，不影响问答主流程。
        """
        rel = self._normalize_rel(rel_path)
        if rel is None or not content:
            return False
        try:
            adapter = await self._get_adapter(user_id, session_id)
            if adapter is None:
                return False
            abs_dir = f"{self._session_dir(session_id)}/uploads"
            await adapter.bash(f"mkdir -p {abs_dir}")
            abs_path = f"{abs_dir}/{rel.rsplit('/', 1)[-1]}"
            await adapter.write(abs_path, content)
            return True
        except Exception:
            logger.warning(
                f"[opensandbox_ws] write_session_file 失败 "
                f"user={user_id} session={session_id} rel={rel_path}",
                exc_info=True,
            )
            return False

    async def stat_session_file(
        self, user_id: str, session_id: str, rel_path: str
    ) -> int | None:
        """返回文件字节大小；文件不存在返回 None。"""
        rel = self._normalize_rel(rel_path)
        if rel is None:
            return None
        try:
            adapter = await self._get_adapter(user_id, session_id)
            if adapter is None:
                return None
            abs_path = f"{self._session_dir(session_id)}/{rel}"
            import shlex
            result = await adapter.bash(
                f"stat -c %s {shlex.quote(abs_path)} 2>/dev/null || true"
            )
            stdout = result.get("stdout", "") if isinstance(result, dict) else ""
            stdout = stdout.strip()
            if not stdout or not stdout.isdigit():
                return None
            return int(stdout)
        except Exception:
            logger.warning(
                f"[opensandbox_ws] stat_session_file 失败 "
                f"user={user_id} session={session_id} rel={rel_path}",
                exc_info=True,
            )
            return None

    async def close(self, workspace_id: str) -> None:
        await self._evict(workspace_id)

    async def close_all(self) -> None:
        self._closing = True
        # 先销毁预热池
        await self._destroy_pool()
        for wid in list(self._cache.keys()):
            await self._evict(wid)
        await self._drain_deferred_clients(force=True)
        logger.info(
            f"[opensandbox_ws] 全部关闭 total_created={self._total_created} "
            f"total_failed={self._total_failed}"
        )

    async def start_sweeper(self) -> None:
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_stop.clear()
        self._sweeper_task = asyncio.create_task(
            self._sweeper_loop(), name="opensandbox-sweeper"
        )
        # 预热池初始化（后台执行，不阻塞启动）
        if self._pool_size > 0:
            asyncio.create_task(self._warm_up())

    async def stop_sweeper(self) -> None:
        self._sweeper_stop.set()
        if self._sweeper_task is not None:
            try:
                await asyncio.wait_for(self._sweeper_task, timeout=10)
            except asyncio.TimeoutError:
                self._sweeper_task.cancel()
            except asyncio.CancelledError:
                pass
            self._sweeper_task = None

    async def _sweeper_loop(self) -> None:
        while not self._sweeper_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._sweeper_stop.wait(), timeout=self._sweeper_interval
                )
            except asyncio.TimeoutError:
                pass
            if self._sweeper_stop.is_set():
                break
            await self._renew_active_workspaces()
            now = time.monotonic()
            expired = [
                wid for wid, e in self._cache.items() if (now - e.last_access) > self._ttl
            ]
            for wid in expired:
                await self._evict(wid)
            await self._drain_deferred_clients()
            if expired:
                logger.info(
                    f"[opensandbox_ws] sweeper 淘汰 {len(expired)} 个过期沙箱 "
                    f"active={len(self._cache)}"
                )

    async def _renew_active_workspaces(self) -> None:
        """长轮次中持续续期共享沙箱；空闲用户不续期。"""
        if self._redis is None:
            return

        async def renew(wid):
            lock = await self._get_lock(wid)
            async with lock:
                try:
                    if not await asyncio.wait_for(self._other_active_runs(wid, ""), timeout=5):
                        return
                    mapped_id = await self._redis_get_map(wid)
                    entry = self._cache.get(wid)
                    if entry is not None and mapped_id in (None, entry.sandbox.id):
                        await self._renew_expiration(entry.sandbox, wid)
                        entry.last_access = time.monotonic()
                    for deferred_wid, sbx in list(self._deferred_clients):
                        if deferred_wid == wid and mapped_id in (None, sbx.id):
                            await self._renew_expiration(sbx, wid)
                except Exception:
                    logger.warning("[opensandbox_ws] 活跃沙箱后台续期失败 wid=%s", wid, exc_info=True)

        workspaces = set(self._cache) | {wid for wid, _ in self._deferred_clients}
        await asyncio.gather(*(renew(wid) for wid in workspaces))
