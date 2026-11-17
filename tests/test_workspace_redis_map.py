"""Shared-sandbox regressions exercising production code without live SDK/Redis."""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_manager():
    source = ROOT / "app/services/opensandbox_workspace_manager.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom)
        and (node.module.startswith("opensandbox") or node.module == "agentscope.skill")
    )]
    module = types.ModuleType("_isolated_workspace_manager")
    module.Sandbox = type("Sandbox", (), {})
    module.ConnectionConfig = object
    module.WriteEntry = object
    module.Skill = type("Skill", (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})
    exec(compile(tree, str(source), "exec"), module.__dict__)
    return module


ws = _load_manager()
ws._PREPARE_RETRY_DELAY = 0
WID = "user-u1"
MAP_KEY = "sandbox_map:user-u1"
LOCK_KEY = "sandbox_lock:user-u1"


class MemoryRedis:
    """Redis expiry and atomic compares used by the real manager and lease."""
    def __init__(self):
        self.data = {}
        self.expires = {}
        self.unavailable = False
        self.fail_publish = False

    def _check(self):
        if self.unavailable:
            raise ConnectionError("Redis unavailable")
        for key, deadline in list(self.expires.items()):
            if deadline <= time.monotonic():
                self.data.pop(key, None)
                self.expires.pop(key, None)

    async def get(self, key):
        self._check()
        return self.data.get(key)

    async def set(self, key, value, *, ex=None, px=None, nx=False):
        self._check()
        if nx and key in self.data:
            return False
        self.data[key] = value
        if ex is not None or px is not None:
            self.expires[key] = time.monotonic() + (ex if ex is not None else px / 1000)
        return True

    async def delete(self, key):
        self._check()
        return int(self.data.pop(key, None) is not None)

    async def scan_iter(self, match=None):
        import fnmatch
        for key in list(self.data):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    async def eval(self, script, numkeys, *args):
        self._check()
        keys, argv = args[:numkeys], args[numkeys:]
        if numkeys == 2:
            if self.fail_publish:
                raise ConnectionError("publish failed")
            if self.data.get(keys[0]) != argv[0] or keys[1] in self.data:
                return 0
            await self.set(keys[1], argv[1], ex=int(argv[2]))
            return 1
        if self.data.get(keys[0]) != argv[0]:
            return 0
        if "pexpire" in script.lower():
            self.expires[keys[0]] = time.monotonic() + int(argv[1]) / 1000
        elif "expire" in script.lower():
            self.expires[keys[0]] = time.monotonic() + int(argv[1])
        elif "del" in script.lower():
            await self.delete(keys[0])
        return 1


class FakeSandbox:
    def __init__(self, sandbox_id, error=None):
        self.id = sandbox_id
        self.workdir = ""
        self.commands = self
        self.error = error
        self.destroyed = False
        self.closed = False
        self.calls = []
        self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=3600)

    async def get_info(self):
        return types.SimpleNamespace(expires_at=self.expires_at, status=types.SimpleNamespace(state="Running"))

    async def run(self, command):
        self.calls.append(command)
        if self.error:
            raise self.error
        return types.SimpleNamespace(exit_code=0)

    async def destroy(self):
        self.destroyed = True

    async def close(self):
        self.closed = True


class RenewableSandbox(FakeSandbox):
    """记录 renew 调用并回写新的到期时间，模拟 SDK 的滑动续期接口。"""

    def __init__(self, sandbox_id, error=None):
        super().__init__(sandbox_id, error=error)
        self.renew_calls = []

    async def renew(self, timeout):
        self.renew_calls.append(timeout)
        self.expires_at = datetime.now(timezone.utc) + timeout
        return types.SimpleNamespace(expires_at=self.expires_at)


def manager(redis=None):
    return ws.OpenSandboxWorkspaceManager(object(), "image", "/data/workspaces", 3600, redis_client=redis)


class SharedWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_reply_lost_does_not_destroy_visible_shared_sandbox(self):
        redis = MemoryRedis()
        mgr, sandbox = manager(redis), FakeSandbox("visible")

        async def published_but_reply_lost(wid, sid, lease, **kwargs):
            await redis.set(MAP_KEY, sid, ex=3600)
            raise ws.CoordinationUnavailable("reply lost after publication")

        with patch.object(ws.Sandbox, "create", AsyncMock(return_value=sandbox), create=True), \
             patch.object(mgr, "_redis_publish_map", published_but_reply_lost):
            with self.assertRaises(ws.CoordinationUnavailable):
                await mgr.create_workspace("u1", "s1")
        self.assertFalse(sandbox.destroyed)
        self.assertEqual(redis.data.get(MAP_KEY), "visible")

    async def test_create_workspace_writes_map_and_releases_own_lock(self):
        redis = MemoryRedis()
        mgr = manager(redis)
        sandbox = FakeSandbox("new")
        with patch.object(ws.Sandbox, "create", AsyncMock(return_value=sandbox), create=True):
            result = await mgr.create_workspace("u1", "s1")
        self.assertIs(result, sandbox)
        self.assertEqual(redis.data.get(MAP_KEY), "new")
        self.assertNotIn(LOCK_KEY, redis.data)
        self.assertIs(mgr._cache[WID].sandbox, sandbox)

    async def test_existing_map_connects_without_creating(self):
        redis = MemoryRedis()
        await redis.set(MAP_KEY, "shared")
        sandbox = FakeSandbox("shared")
        mgr = manager(redis)
        with patch.object(ws.Sandbox, "connect", AsyncMock(return_value=sandbox), create=True), \
             patch.object(ws.Sandbox, "create", AsyncMock(side_effect=AssertionError("duplicate")), create=True):
            self.assertIs(await mgr.create_workspace("u1", "s1"), sandbox)
        self.assertEqual(redis.data[MAP_KEY], "shared")

    async def test_transient_connect_failure_preserves_mapping_and_does_not_create(self):
        redis = MemoryRedis()
        await redis.set(MAP_KEY, "shared")
        mgr = manager(redis)
        with patch.object(ws.Sandbox, "connect", AsyncMock(side_effect=ConnectionError("reset")), create=True), \
             patch.object(ws.Sandbox, "create", AsyncMock(return_value=FakeSandbox("duplicate")), create=True):
            with self.assertRaises(Exception):
                await mgr.create_workspace("u1", "s1")
        self.assertEqual(redis.data[MAP_KEY], "shared")
        self.assertEqual(mgr._total_created, 0)

    async def test_shutdown_closes_local_client_without_destroying_shared_sandbox(self):
        redis = MemoryRedis()
        await redis.set(MAP_KEY, "shared")
        sandbox = FakeSandbox("shared")
        mgr = manager(redis)
        mgr._cache[WID] = ws._Entry(sandbox, "u1")
        await mgr.close_all()
        self.assertFalse(sandbox.destroyed)
        self.assertTrue(sandbox.closed)
        self.assertEqual(redis.data[MAP_KEY], "shared")
        self.assertEqual(mgr._cache, {})

    async def test_expired_local_cache_does_not_destroy_globally_active_sandbox(self):
        redis = MemoryRedis()
        await redis.set(MAP_KEY, "shared")
        stale, remote = FakeSandbox("shared"), FakeSandbox("shared")
        mgr = manager(redis)
        entry = ws._Entry(stale, "u1")
        entry.last_access -= 7200
        mgr._cache[WID] = entry
        with patch.object(ws.Sandbox, "connect", AsyncMock(return_value=remote), create=True):
            result = await mgr.get_workspace("u1", "s2")
        self.assertFalse(stale.destroyed)
        self.assertTrue(stale.closed)
        self.assertIs(result, remote)
        self.assertEqual(redis.data[MAP_KEY], "shared")

    async def test_reuse_renews_shared_sandbox_and_extends_map_ttl(self):
        """复用活跃共享沙箱时同时续期服务端到期时间与 Redis 映射 TTL。

        只续服务端不刷映射，映射会先过期，其他 Pod 会误判沙箱消失而另建。
        """
        redis = MemoryRedis()
        await redis.set(MAP_KEY, "shared", ex=120)
        sandbox = RenewableSandbox("shared")
        mgr = manager(redis)
        mgr._cache[WID] = ws._Entry(sandbox, "u1")

        before = redis.expires[MAP_KEY] - time.monotonic()
        await mgr.get_workspace("u1", "s2")
        after = redis.expires[MAP_KEY] - time.monotonic()

        self.assertEqual(len(sandbox.renew_calls), 1)
        self.assertEqual(sandbox.renew_calls[0].total_seconds(), 3600)
        self.assertGreater(after, before)
        self.assertAlmostEqual(after, 3600 - 60, delta=5)

    async def test_stale_cached_id_cannot_overwrite_new_mapping(self):
        redis = MemoryRedis()
        await redis.set(MAP_KEY, "new-owner")
        stale, current = FakeSandbox("stale"), FakeSandbox("new-owner")
        mgr = manager(redis)
        mgr._cache[WID] = ws._Entry(stale, "u1")
        with patch.object(ws.Sandbox, "connect", AsyncMock(return_value=current), create=True):
            self.assertIs(await mgr.get_workspace("u1", "s2"), current)
        self.assertEqual(stale.calls, [])
        self.assertTrue(stale.closed)
        self.assertEqual(redis.data[MAP_KEY], "new-owner")

    async def test_transient_cached_probe_does_not_delete_or_recreate_mapping(self):
        redis = MemoryRedis()
        await redis.set(MAP_KEY, "shared")
        sandbox = FakeSandbox("shared", ConnectionError("connection reset"))
        mgr = manager(redis)
        mgr._cache[WID] = ws._Entry(sandbox, "u1")
        with patch.object(ws.Sandbox, "create", AsyncMock(return_value=FakeSandbox("duplicate")), create=True):
            with self.assertRaises(Exception):
                await mgr.create_workspace("u1", "s2")
        self.assertEqual(redis.data[MAP_KEY], "shared")
        self.assertFalse(sandbox.destroyed)

    async def test_proven_gone_mapping_can_be_replaced(self):
        redis = MemoryRedis()
        await redis.set(MAP_KEY, "gone")
        mgr = manager(redis)
        new = FakeSandbox("new")
        with patch.object(ws.Sandbox, "connect", AsyncMock(side_effect=RuntimeError("SANDBOX_NOT_FOUND")), create=True), \
             patch.object(ws.Sandbox, "create", AsyncMock(return_value=new), create=True):
            self.assertIs(await mgr.create_workspace("u1", "s2"), new)
        self.assertEqual(redis.data[MAP_KEY], "new")

    async def test_proven_gone_old_id_cannot_delete_replacement_mapping(self):
        redis = MemoryRedis()
        await redis.set(MAP_KEY, "old")
        mgr = manager(redis)
        async def replacement(*args, **kwargs):
            await redis.set(MAP_KEY, "replacement")
            raise RuntimeError("SANDBOX_NOT_FOUND")
        with patch.object(ws.Sandbox, "connect", replacement, create=True):
            await mgr._connect_from_redis(WID, "u1", "s1")
        self.assertEqual(redis.data[MAP_KEY], "replacement")

    async def test_redis_outage_fails_closed_before_sandbox_creation(self):
        redis = MemoryRedis()
        redis.unavailable = True
        mgr = manager(redis)
        with patch.object(ws.Sandbox, "create", AsyncMock(return_value=FakeSandbox("duplicate")), create=True):
            with self.assertRaises(Exception):
                await mgr.create_workspace("u1", "s1")
        self.assertEqual(mgr._total_created, 0)
        self.assertEqual(mgr._cache, {})

    async def test_lock_wait_timeout_does_not_create_unlocked(self):
        redis = MemoryRedis()
        await redis.set(LOCK_KEY, "other-owner", ex=60)
        mgr = manager(redis)
        with patch.object(ws, "_DIST_LOCK_WAIT_TIMEOUT", 0), \
             patch.object(ws.Sandbox, "create", AsyncMock(return_value=FakeSandbox("duplicate")), create=True):
            with self.assertRaises(Exception):
                await mgr.create_workspace("u1", "s1")
        self.assertEqual(mgr._cache, {})
        self.assertEqual(redis.data[LOCK_KEY], "other-owner")

    async def test_failed_fenced_publish_destroys_unpublished_sandbox(self):
        redis = MemoryRedis()
        redis.fail_publish = True
        mgr = manager(redis)
        sandbox = FakeSandbox("unpublished")
        with patch.object(ws.Sandbox, "create", AsyncMock(return_value=sandbox), create=True):
            with self.assertRaises(Exception):
                await mgr.create_workspace("u1", "s1")
        self.assertTrue(sandbox.destroyed)
        self.assertEqual(mgr._cache, {})
        self.assertNotIn(MAP_KEY, redis.data)

    async def test_slow_create_renews_lease_and_other_manager_reuses_one_sandbox(self):
        redis = MemoryRedis()
        first, second = manager(redis), manager(redis)
        sandbox = FakeSandbox("shared")
        started = asyncio.Event()
        creations = []

        async def slow_create(*args, **kwargs):
            creations.append("shared")
            started.set()
            await asyncio.sleep(0.18)  # Three times the initial creation lease.
            return sandbox

        with patch.object(ws, "_DIST_LOCK_TTL", 0.06), \
             patch.object(ws, "_DIST_LOCK_POLL_INTERVAL", 0.01), \
             patch.object(ws, "_DIST_LOCK_WAIT_TIMEOUT", 1), \
             patch.object(ws.Sandbox, "create", slow_create, create=True), \
             patch.object(ws.Sandbox, "connect", AsyncMock(return_value=sandbox), create=True):
            task = asyncio.create_task(first.create_workspace("u1", "s1"))
            await started.wait()
            a, b = await asyncio.gather(task, second.create_workspace("u1", "s2"))
        self.assertIs(a, sandbox)
        self.assertIs(b, sandbox)
        self.assertEqual(creations, ["shared"])
        self.assertEqual(redis.data[MAP_KEY], "shared")

    async def test_lost_creation_lease_neither_publishes_nor_deletes_new_owner_lock(self):
        redis = MemoryRedis()
        mgr = manager(redis)
        sandbox = FakeSandbox("unpublished")

        async def stolen_while_creating(*args, **kwargs):
            await redis.set(LOCK_KEY, "replacement-owner", ex=60)
            return sandbox

        with patch.object(ws.Sandbox, "create", stolen_while_creating, create=True):
            with self.assertRaises(ws.LeaseLost):
                await mgr.create_workspace("u1", "s1")
        self.assertTrue(sandbox.destroyed)
        self.assertEqual(redis.data[LOCK_KEY], "replacement-owner")
        self.assertNotIn(MAP_KEY, redis.data)
        self.assertEqual(mgr._cache, {})

    async def test_fenced_publish_does_not_overwrite_a_mapping_created_during_initialization(self):
        redis = MemoryRedis()
        mgr = manager(redis)
        sandbox = FakeSandbox("unpublished")

        async def replacement_during_initialization(*args, **kwargs):
            await redis.set(MAP_KEY, "replacement")
            return sandbox

        with patch.object(ws.Sandbox, "create", replacement_during_initialization, create=True):
            with self.assertRaises(ws.LeaseLost):
                await mgr.create_workspace("u1", "s1")
        self.assertTrue(sandbox.destroyed)
        self.assertEqual(redis.data[MAP_KEY], "replacement")
        self.assertEqual(mgr._cache, {})

    async def test_failed_shared_initialization_cleans_up_without_publishing(self):
        redis = MemoryRedis()
        mgr = manager(redis)
        sandbox = FakeSandbox("unready", ConnectionError("execd reset"))
        with patch.object(ws.Sandbox, "create", AsyncMock(return_value=sandbox), create=True):
            with self.assertRaises(Exception):
                await mgr.create_workspace("u1", "s1")
        self.assertTrue(sandbox.destroyed)
        self.assertNotIn(MAP_KEY, redis.data)
        self.assertEqual(mgr._cache, {})

    async def test_no_redis_preserves_single_process_creation_and_destruction(self):
        mgr = manager()
        sandbox = FakeSandbox("local")
        with patch.object(ws.Sandbox, "create", AsyncMock(return_value=sandbox), create=True):
            self.assertIs(await mgr.create_workspace("u1", "s1"), sandbox)
        await mgr.close_all()
        self.assertTrue(sandbox.destroyed)


if __name__ == "__main__":
    unittest.main()
