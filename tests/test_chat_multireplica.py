"""Cross-instance chat regressions; runnable with unittest without app SDKs."""
import ast
import asyncio
from contextlib import aclosing
import json
import logging
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class MemoryRedis:
    def __init__(self):
        self.data = {}
        self.expires = {}

    async def get(self, key):
        if time.monotonic() >= self.expires.get(key, float("inf")):
            self.data.pop(key, None)
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False, px=None):
        if nx and await self.get(key) is not None:
            return False
        self.data[key] = value
        self.expires[key] = time.monotonic() + (px / 1000 if px else ex or 3600)
        return True

    async def delete(self, key):
        return self.data.pop(key, None) is not None

    async def exists(self, key):
        return await self.get(key) is not None

    async def eval(self, script, count, *args):
        key, token = args[0], args[count]
        if await self.get(key) != token:
            return 0
        if "chat_stop_run" in script:
            await self.set(args[1], "1", ex=300)
        elif "lease_renew" in script:
            self.expires[key] = time.monotonic() + int(args[2]) / 1000
        elif "lease_release" in script:
            await self.delete(key)
        else:
            raise AssertionError("unsupported script")
        return 1


class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


def load_routes():
    tree = ast.parse(Path("app/routes/chat.py").read_text(encoding="utf-8-sig"))
    body = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = []
            node.args.defaults = []
            body.append(node)
    body.insert(0, ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
    env = dict(asyncio=asyncio, aclosing=aclosing, json=json, logging=logging, logger=logging.getLogger(__name__),
               HTTPException=HttpError, SSE_HEARTBEAT_INTERVAL=0.01, _DONE=object(),
               _SSE_HEARTBEAT=": ping\n\n", _DETACHED_TASKS=set())
    # Coordination module has no SDK dependencies; route import boundaries alone are replaced.
    try:
        from app.services import chat_runtime
        env.update({name: getattr(chat_runtime, name) for name in (
            "ChatRun", "RunBusy", "CoordinationUnavailable", "LeaseLost", "request_stop",
            "authorize_active_run")})
    except ImportError:
        pass
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "chat.py", "exec"), env)
    return env


class ChatRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _disconnect_then_cancel(self, reason):
        from app.services import chat_runtime
        routes, redis = load_routes(), MemoryRedis()
        started, finalized = asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            try:
                started.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                yield 'data: {"type":"user_abort"}\n\n'
            finally:
                finalized.set()

        routes.update(
            generate_response=generate,
            cancel_feedback_shared=AsyncMock(),
            StreamingResponse=lambda content, **kwargs: SimpleNamespace(content=content, **kwargs),
            BackgroundTask=lambda func, **kwargs: lambda: func(**kwargs),
        )
        state = SimpleNamespace(
            redis_client=redis, chat_tasks={},
            session_service=SimpleNamespace(get_or_create_session=AsyncMock(return_value="s")),
            orchestrator_service=None, langfuse_service=None,
        )
        body = SimpleNamespace(session_id="s", messages=[], agent_id=None,
                               search_enabled=False, skills=[], knowledge_bases=[])
        request = SimpleNamespace(headers={}, app=SimpleNamespace(state=state))
        with patch.object(chat_runtime, "_STOP_POLL_INTERVAL", 0.001):
            response = await routes["chat"](request, body, {"user_id": "u"})
            run = state.chat_tasks["s"]
            consumer = asyncio.create_task(self._collect(response.content))
            await asyncio.wait_for(started.wait(), 1)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
            await response.background()
            self.assertIn("s", state.chat_tasks)
            await run.lease.assert_owned()
            if reason == "user":
                self.assertTrue(await chat_runtime.request_stop(redis, "s", "u"))
            else:
                run.lease._mark_lost()
            await asyncio.wait_for(finalized.wait(), 1)
            pending = list(routes["_DETACHED_TASKS"])
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 1)
            await asyncio.sleep(0)
            self.assertEqual(state.chat_tasks, {})
            self.assertIsNone(await redis.get(run.key))
            self.assertIsNone(run.lease._renew_task)
            self.assertEqual(run.cancel_event.reason, reason)
            await response.background()
            routes["cancel_feedback_shared"].assert_awaited_once_with("s")
            self.assertEqual(routes["_DETACHED_TASKS"], set())

    @staticmethod
    async def _collect(source):
        return [chunk async for chunk in source]

    async def test_detached_silent_generation_still_obeys_cross_pod_stop(self):
        await self._disconnect_then_cancel("user")

    async def test_detached_silent_generation_stops_on_lease_loss(self):
        await self._disconnect_then_cancel("lease_lost")

    async def test_cancel_before_pump_starts_still_finishes_once(self):
        routes, event = load_routes(), asyncio.Event()
        event.set()
        cleanup = AsyncMock()
        async def source():
            await asyncio.Event().wait()
            yield "unused"
        await asyncio.wait_for(self._collect(
            routes["_with_heartbeat"](source(), event, cleanup, None)), 1)
        cleanup.assert_awaited_once()

    async def test_disconnect_cannot_strand_renewing_chat_lease(self):
        from app.services.chat_runtime import ChatRun
        redis = MemoryRedis()
        run = ChatRun(redis, "s", "u")
        await run.open()
        started, unblock = asyncio.Event(), asyncio.Event()
        original_delete = redis.delete

        async def slow_delete(key):
            if key == run.stop_key:
                started.set()
                await unblock.wait()
            return await original_delete(key)

        redis.delete = slow_delete
        cleanup = asyncio.create_task(run.close())
        await started.wait()
        cleanup.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cleanup
        unblock.set()
        await asyncio.wait_for(run.close(), 1)
        self.assertIsNone(await redis.get(run.key))
        self.assertIsNone(run.lease._renew_task)

    async def test_idle_stop_does_not_poison_next_round(self):
        redis = MemoryRedis()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            redis_client=redis, chat_tasks={})))
        result = await load_routes()["stop_chat"](request, SimpleNamespace(session_id="s"), {"user_id": "u"})
        self.assertFalse(result["ok"])
        self.assertEqual(redis.data, {})

    async def test_cross_pod_stop_checks_owner_before_writing(self):
        redis = MemoryRedis()
        redis.data["chat_run:s"] = json.dumps({"user_id": "owner", "run_id": "r"})
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            redis_client=redis, chat_tasks={})))
        with self.assertRaises(HttpError) as error:
            await load_routes()["stop_chat"](request, SimpleNamespace(session_id="s"), {"user_id": "other"})
        self.assertEqual(error.exception.status_code, 403)
        self.assertNotIn("chat_stop:s:r", redis.data)

    async def test_cross_pod_stop_returns_success_for_current_round(self):
        from app.services.chat_runtime import ChatRun
        redis = MemoryRedis()
        owner = ChatRun(redis, "s", "u")
        await owner.open()
        try:
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis_client=redis, chat_tasks={})))
            response = await load_routes()["stop_chat"](request, SimpleNamespace(session_id="s"), {"user_id": "u"})
            self.assertTrue(response["ok"])
            self.assertTrue(await redis.exists(owner.stop_key))
        finally:
            await owner.close()

    async def test_same_session_cannot_run_on_two_pods(self):
        from app.services.chat_runtime import ChatRun, RunBusy
        redis = MemoryRedis()
        first, second = ChatRun(redis, "s", "u"), ChatRun(redis, "s", "u")
        await first.open()
        try:
            with self.assertRaises(RunBusy):
                await second.open()
            await second.close()
            await first.lease.assert_owned()
        finally:
            await first.close()

    async def test_old_cleanup_does_not_release_new_run_or_stop(self):
        from app.services.chat_runtime import ChatRun
        redis = MemoryRedis()
        old = ChatRun(redis, "s", "u")
        await old.open()
        await redis.delete(old.key)  # simulate old lease expiry
        new = ChatRun(redis, "s", "u")
        await new.open()
        await redis.set(new.stop_key, "1", ex=300)
        try:
            await old.close()
            await new.lease.assert_owned()
            self.assertTrue(await redis.exists(new.stop_key))
        finally:
            await new.close()

    async def test_lease_renewal_keeps_slow_round_exclusive(self):
        from app.services.redis_lease import RedisLease
        redis = MemoryRedis()
        lease = RedisLease(redis, "key", ttl=0.09)
        await lease.acquire()
        try:
            await asyncio.sleep(0.22)
            other = RedisLease(redis, "key", ttl=0.09)
            self.assertFalse(await other.acquire())
            await lease.assert_owned()
        finally:
            await lease.release()

    async def test_redis_failure_does_not_allow_uncoordinated_chat(self):
        from app.services.chat_runtime import ChatRun, CoordinationUnavailable
        class BrokenRedis(MemoryRedis):
            async def set(self, *args, **kwargs):
                raise OSError("redis unavailable")
        with self.assertRaises(CoordinationUnavailable):
            await ChatRun(BrokenRedis(), "s", "u").open()

    async def test_stop_interrupts_silent_upstream_and_keeps_abort_event(self):
        event, started = asyncio.Event(), asyncio.Event()
        async def source():
            try:
                started.set()
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                yield 'data: {"type":"user_abort"}\n\n'
        async def collect():
            # load_routes 会清空函数默认值，调用需显式传入 on_finish/on_detach。
            return [chunk async for chunk in load_routes()["_with_heartbeat"](source(), event, None, None)]
        task = asyncio.create_task(collect())
        await started.wait()
        event.set()
        output = await asyncio.wait_for(task, 1)
        self.assertTrue(any("user_abort" in chunk for chunk in output))

    async def test_client_disconnect_defers_teardown_until_generation_ends(self):
        """客户端断连：生成转后台跑完，收尾必须在生成结束后执行。

        若收尾（释放租约 + 移出运行表）在断连瞬间提前执行，同一会话就能被
        并发开启新一轮（single-flight 失效），也会提前取消正在等待的用户反馈。
        """
        routes = load_routes()
        finished, teardown, detached = asyncio.Event(), [], {}

        async def source():
            try:
                for i in range(3):
                    await asyncio.sleep(0.02)
                    yield f"data: {i}\n\n"
            finally:
                finished.set()

        async def on_finish():
            teardown.append("cleanup")

        async def collect():
            return [chunk async for chunk in routes["_with_heartbeat"](
                source(), None, on_finish, lambda task: detached.setdefault("task", task))]

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.03)  # 生成进行中，模拟客户端断连
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(finished.is_set())
        self.assertEqual(teardown, [])  # 断连瞬间不得收尾
        pump = detached["task"]
        await asyncio.wait_for(pump, 2)  # 后台生成跑完
        self.assertFalse(pump.cancelled())
        self.assertTrue(finished.is_set())
        self.assertEqual(teardown, ["cleanup"])  # 生成结束才收尾一次


if __name__ == "__main__":
    unittest.main()
