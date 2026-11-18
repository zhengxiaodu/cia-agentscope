"""Feedback callbacks across independent module instances (Pods), stdlib only."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]


def load_service():
    name = "feedback_pod_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "app/services/feedback_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_tool(service):
    sdk_message = types.ModuleType("agentscope.message")
    sdk_message.TextBlock = dict
    sdk_tool = types.ModuleType("agentscope.tool")
    sdk_tool.FunctionTool = lambda fn, **kwargs: fn
    sdk_tool.ToolChunk = lambda **kwargs: types.SimpleNamespace(**kwargs)
    spec = importlib.util.spec_from_file_location(
        "feedback_tool_" + uuid.uuid4().hex, ROOT / "tools/feedback_tools.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "agentscope.message": sdk_message,
        "agentscope.tool": sdk_tool,
        "app.services.feedback_service": service,
    }):
        spec.loader.exec_module(module)
    return module


class FakeRedis:
    """Atomic Redis operations used here; results are durable until consumed/TTL."""
    def __init__(self):
        self.values = {}
        self.fail = False

    def _get(self, key):
        if self.fail:
            raise ConnectionError("redis unavailable")
        stored = self.values.get(key)
        if stored is None:
            return None
        raw, expires = stored
        if expires <= time.monotonic():
            del self.values[key]
            return None
        return raw

    async def get(self, key):
        return self._get(key)

    async def set(self, key, value, *, nx=False, ex=None):
        existing = self._get(key)
        if nx and existing is not None:
            return False
        self.values[key] = (value, time.monotonic() + ex)
        return True

    async def eval(self, script, number_of_keys, key, *args):
        assert number_of_keys == 1
        raw = self._get(key)
        if raw is None:
            return None if "feedback:consume" in script else 0
        state = json.loads(raw)
        if "feedback:resolve" in script:
            if state["feedback_id"] != args[0] or state["status"] != "pending":
                return 0
            state["status"] = "resolved"
            state["result_json"] = args[1]
            self.values[key] = (json.dumps(state), self.values[key][1])
            return 1
        if "feedback:consume" in script:
            if state["owner_token"] != args[0]:
                return None
            del self.values[key]
            return raw
        raise AssertionError("unexpected Lua operation")


class FeedbackMultireplicaTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_can_arrive_immediately_after_card_chunk(self):
        service = load_service()
        tool = load_tool(service)
        stream = tool._feedback_card_stream("session", "confirm", {}, timeout=1)
        try:
            card = json.loads((await anext(stream)).content[0]["text"])
            accepted = service.resolve_feedback(
                "session", card["schema"]["feedbackId"], {"action": "confirm"}
            )
            self.assertTrue(accepted, "card must register before it can reach the UI")
            result = json.loads((await anext(stream)).content[0]["text"])
            self.assertEqual(result, {"action": "confirm"})
        finally:
            await stream.aclose()

    def make_pods(self):
        redis = FakeRedis()
        owner, callback = load_service(), load_service()
        for service in (owner, callback):
            service.configure_feedback(redis)
            service.FEEDBACK_POLL_INTERVAL_SECONDS = 0.001
        return redis, owner, callback

    async def test_callback_on_another_pod_is_one_shot_and_durable(self):
        redis, owner, callback = self.make_pods()
        async with owner.feedback_registration("session", "card", timeout=1) as pending:
            self.assertFalse(await callback.resolve_feedback_shared("session", "old-card", {}))
            result = {"action": "submit", "payload": {"value": "confirmed", "list": [], "object": {}}}
            accepted = await asyncio.gather(*[
                callback.resolve_feedback_shared("session", "card", result)
                for _ in range(5)
            ])
            self.assertEqual(accepted.count(True), 1)
            # Resolve before waiting: a lost pubsub notification must not lose the result.
            self.assertEqual(await owner.wait_registered_feedback(pending), result)
        self.assertEqual(redis.values, {})

    async def test_active_waiter_polls_callback_from_another_pod(self):
        _, owner, callback = self.make_pods()
        async with owner.feedback_registration("session", "card", timeout=1) as pending:
            waiter = asyncio.create_task(owner.wait_registered_feedback(pending))
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            self.assertTrue(await callback.resolve_feedback_shared("session", "card", {"action": "ok"}))
            self.assertEqual(await asyncio.wait_for(waiter, 1), {"action": "ok"})

    async def test_timeout_keeps_callback_accepted_between_last_read_and_close(self):
        redis, owner, callback = self.make_pods()
        original_get = redis.get

        async def callback_after_read(key):
            raw = await original_get(key)
            self.assertTrue(await callback.resolve_feedback_shared("session", "card", {"action": "ok"}))
            return raw

        async with owner.feedback_registration("session", "card", timeout=0) as pending:
            redis.get = callback_after_read
            self.assertEqual(await owner.wait_registered_feedback(pending), {"action": "ok"})

    async def test_expired_owner_cleanup_does_not_remove_new_card(self):
        redis, owner, other = self.make_pods()
        old_context = owner.feedback_registration("session", "old", timeout=1)
        old = await old_context.__aenter__()
        try:
            key = next(iter(redis.values))
            raw, _ = redis.values[key]
            redis.values[key] = (raw, time.monotonic() - 1)
            async with other.feedback_registration("session", "new", timeout=1) as new:
                self.assertEqual((await owner.wait_registered_feedback(old))["status"], "cancelled")
                await old_context.__aexit__(None, None, None)
                self.assertTrue(await other.resolve_feedback_shared("session", "new", {}))
                self.assertEqual(await other.wait_registered_feedback(new), {})
        finally:
            await old_context.__aexit__(None, None, None)

    async def test_existing_waiter_keeps_registered_redis_client(self):
        redis, owner, callback = self.make_pods()
        async with owner.feedback_registration("session", "card", timeout=1) as pending:
            owner.configure_feedback(FakeRedis())
            self.assertTrue(await callback.resolve_feedback_shared("session", "card", {}))
            self.assertEqual(await owner.wait_registered_feedback(pending), {})
        self.assertEqual(redis.values, {})

    async def test_other_pod_cannot_register_second_pending_card(self):
        _, owner, other = self.make_pods()
        async with owner.feedback_registration("session", "card", timeout=1):
            with self.assertRaises(other.FeedbackAlreadyPending):
                async with other.feedback_registration("session", "card2", timeout=1):
                    self.fail("duplicate shared slot")
            self.assertTrue(await other.resolve_feedback_shared("session", "card", {}))

    async def test_cancel_without_local_pending_does_not_cancel_remote_owner(self):
        _, owner, other = self.make_pods()
        async with owner.feedback_registration("session", "card", timeout=1) as pending:
            await other.cancel_feedback_shared("session")
            self.assertTrue(await other.resolve_feedback_shared("session", "card", {}))
            self.assertEqual(await owner.wait_registered_feedback(pending), {})

    async def test_cancel_and_old_context_exit_preserve_replacement(self):
        redis, owner, other = self.make_pods()
        async with owner.feedback_registration("session", "old", timeout=1) as old:
            await owner.cancel_feedback_shared("session")
            async with other.feedback_registration("session", "new", timeout=1) as new:
                self.assertEqual((await owner.wait_registered_feedback(old))["status"], "cancelled")
                await owner.cancel_feedback_shared("session")
                self.assertTrue(await owner.resolve_feedback_shared("session", "new", {"action": "ok"}))
                self.assertEqual(await other.wait_registered_feedback(new), {"action": "ok"})
        self.assertEqual(redis.values, {})

    async def test_timeout_and_task_cancellation_release_owned_slot(self):
        redis, owner, other = self.make_pods()
        result = await owner.wait_feedback("session", "card", timeout=0.001)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(redis.values, {})
        registered = asyncio.Event()

        async def wait_and_cancel():
            async with owner.feedback_registration("session", "next", timeout=1) as pending:
                registered.set()
                return await owner.wait_registered_feedback(pending)

        task = asyncio.create_task(wait_and_cancel())
        await registered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(redis.values, {})
        self.assertFalse(await other.resolve_feedback_shared("session", "next", {}))

    async def test_shared_registration_resolve_and_wait_fail_clearly_on_redis_error(self):
        redis, owner, callback = self.make_pods()
        redis.fail = True
        with self.assertLogs(owner.logger, level="ERROR"):
            with self.assertRaises(owner.FeedbackBackendError):
                await owner.wait_feedback("session", "card", timeout=1)
        self.assertEqual(owner._pending, {})
        with self.assertRaises(callback.FeedbackBackendError):
            await callback.resolve_feedback_shared("session", "card", {})
        redis.fail = False
        async with owner.feedback_registration("session", "next", timeout=1) as pending:
            redis.fail = True
            try:
                with self.assertRaises(owner.FeedbackBackendError):
                    await owner.wait_registered_feedback(pending)
            finally:
                redis.fail = False

    async def test_shared_card_is_registered_before_first_chunk(self):
        redis, owner, callback = self.make_pods()
        stream = load_tool(owner)._feedback_card_stream("session", "confirm", {}, timeout=1)
        try:
            card = json.loads((await anext(stream)).content[0]["text"])
            self.assertTrue(await callback.resolve_feedback_shared(
                "session", card["schema"]["feedbackId"], {"action": "confirm"}
            ))
            self.assertEqual(json.loads((await anext(stream)).content[0]["text"]), {"action": "confirm"})
        finally:
            await stream.aclose()
        self.assertEqual(redis.values, {})

    async def test_closing_tool_after_card_releases_slot(self):
        redis, owner, _ = self.make_pods()
        stream = load_tool(owner)._feedback_card_stream("session", "confirm", {}, timeout=1)
        await anext(stream)
        self.assertTrue(redis.values)
        await stream.aclose()
        self.assertEqual(redis.values, {})

    async def test_closing_public_tool_after_card_closes_inner_generator(self):
        redis, owner, _ = self.make_pods()
        tool = load_tool(owner).create_feedback_tool("session")
        stream = tool("confirm", {})
        await anext(stream)
        self.assertTrue(redis.values)
        await stream.aclose()
        self.assertEqual(redis.values, {})


if __name__ == "__main__":
    unittest.main()
