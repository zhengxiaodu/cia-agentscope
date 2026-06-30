"""Docker 工作区管理器：按 session_id 分配、缓存、TTL 回收 Docker workspace。

自研管理器（官方 WorkspaceManager 绑定 agentservice 无法复用），底层每个 workspace
复用 agentscope SDK 的 DockerWorkspace。隔离策略：同一 session_id 复用同一 workspace，
不同 session_id 各自独立容器；空闲超 TTL 的 workspace 被后台 sweeper 惰性/周期淘汰并销毁。
"""
import asyncio
import logging
import os
import time
from typing import Optional

from agentscope.workspace import DockerWorkspace

logger = logging.getLogger(__name__)


class _Entry:
    __slots__ = ("workspace", "last_access", "user_id")

    def __init__(self, workspace, user_id: str):
        self.workspace = workspace
        self.last_access = time.monotonic()
        self.user_id = user_id


class DockerWorkspaceManager:
    def __init__(self, base_image: str, basedir: str, ttl: float):
        self._base_image = base_image
        self._basedir = basedir
        self._ttl = ttl
        self._cache: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._struct_lock = asyncio.Lock()
        self._sweeper_task: Optional[asyncio.Task] = None
        self._sweeper_stop = asyncio.Event()
        self._sweeper_interval = max(30.0, min(self._ttl / 2, 300.0))

    @staticmethod
    def _workspace_id(session_id: str) -> str:
        return session_id

    def _session_dir(self, session_id: str) -> str:
        return os.path.join(self._basedir, session_id)

    async def _get_lock(self, wid: str) -> asyncio.Lock:
        async with self._struct_lock:
            lock = self._locks.get(wid)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[wid] = lock
            return lock

    async def _evict_locked(self, wid: str) -> None:
        entry = self._cache.pop(wid, None)
        if entry is not None:
            try:
                await entry.workspace.close()
            except Exception:
                logger.exception(f"[workspace_manager] 关闭工作区失败 wid={wid}")
            logger.info(f"[workspace_manager] 淘汰工作区 wid={wid}")

    async def _evict(self, wid: str) -> None:
        lock = await self._get_lock(wid)
        async with lock:
            await self._evict_locked(wid)

    async def create_workspace(
        self, user_id: str, session_id: str, skill_dirs: list[str]
    ) -> DockerWorkspace:
        wid = self._workspace_id(session_id)
        lock = await self._get_lock(wid)
        async with lock:
            # Double-check：另一并发 waiter 可能已创建；命中且未过期则直接复用。
            entry = self._cache.get(wid)
            if entry is not None and (time.monotonic() - entry.last_access) <= self._ttl:
                entry.last_access = time.monotonic()
                return entry.workspace
            session_dir = self._session_dir(session_id)
            os.makedirs(session_dir, exist_ok=True)
            valid: list[str] = []
            for d in skill_dirs or []:
                if d and os.path.isdir(d):
                    valid.append(d)
                else:
                    logger.warning(f"[workspace_manager] 技能目录不存在，跳过: {d}")
            ws = DockerWorkspace(
                base_image=self._base_image,
                host_workdir=session_dir,
                skill_paths=valid or None,
                default_mcps=[],
            )
            await ws.initialize()
            self._cache[wid] = _Entry(ws, user_id)
            logger.info(f"[workspace_manager] 创建工作区 wid={wid} skills={len(valid)}")
            return ws

    async def get_workspace(
        self, user_id: str, session_id: str
    ) -> Optional[DockerWorkspace]:
        wid = self._workspace_id(session_id)
        lock = await self._get_lock(wid)
        async with lock:
            entry = self._cache.get(wid)
            if entry is None:
                return None
            # 惰性淘汰：超 TTL 则销毁底层资源并返回 None。
            if (time.monotonic() - entry.last_access) > self._ttl:
                await self._evict_locked(wid)
                return None
            entry.last_access = time.monotonic()
            return entry.workspace

    async def close(self, workspace_id: str) -> None:
        await self._evict(workspace_id)

    async def close_all(self) -> None:
        for wid in list(self._cache.keys()):
            await self._evict(wid)

    async def start_sweeper(self) -> None:
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_stop.clear()
        self._sweeper_task = asyncio.create_task(
            self._sweeper_loop(), name="workspace-sweeper"
        )

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
            now = time.monotonic()
            expired = [
                wid for wid, e in self._cache.items() if (now - e.last_access) > self._ttl
            ]
            for wid in expired:
                await self._evict(wid)
