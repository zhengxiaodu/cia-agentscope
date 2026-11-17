"""Execute production stream loops with ContextVar spans, without optional SDKs."""
import ast
import asyncio
from contextlib import AsyncExitStack, aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sys
import time
import traceback
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from app.services.redis_lease import RedisLease, LeaseLost
from tests.test_chat_multireplica import MemoryRedis, load_routes

ROOT = Path(__file__).resolve().parents[1]


def methods(path, names, env, class_name=None):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    if class_name:
        tree = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in names]

    class RemoveImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None

        def visit_Import(self, node):
            return None

    for node in nodes:
        node.decorator_list = []
        RemoveImports().visit(node)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])),
                 path, "exec"), env)
    return {name: env[name] for name in names}


@dataclass
class Result:
    intent_id: str
    agent_id: str
    success: bool = True
    output: str = ""
    final_state: object = None
    metadata: object = None


class Event:
    pass


class ReplyStart:
    pass


class Tracer:
    def __init__(self, *args, **kwargs):
        pass

    def on_event(self, event):
        pass

    def close(self):
        pass

    def summary(self):
        return {}

    def consume_citations(self):
        return []

    def consume_bocha_sum(self):
        return []


class Spans:
    enabled = False  # Disable standalone observations; keep start_span active.

    def __init__(self):
        self.current = ContextVar("test_span", default="outside")
        self.active = set()
        self.closed = []
        self.errors = []

    @contextmanager
    def start_span(self, name, **kwargs):
        task = asyncio.current_task()
        token = self.current.set(name)
        marker = (name, task)
        self.active.add(marker)
        try:
            yield NS(update=lambda **kw: None)
        finally:
            try:
                if asyncio.current_task() is not task:
                    raise AssertionError("span closed in another task")
                if self.current.get() != name:
                    raise AssertionError("parent closed before its child")
                self.current.reset(token)
            except Exception as exc:
                self.errors.append(exc)
            self.active.discard(marker)
            self.closed.append(name)


def streams(mode, behavior, spans, intent_count=1):
    async def reply_stream(message):
        with spans.start_span("sdk"):
            yield Event()
            if behavior == "blocked":
                await asyncio.Event().wait()
            if behavior == "error":
                raise RuntimeError("agent failure")

    agent = NS(reply_stream=reply_stream, state=NS(model_dump=lambda **kw: {}, context=[]))
    factory = NS(create_for_agent=lambda **kw: agent)
    env = dict(
        asyncio=asyncio, aclosing=aclosing, AsyncExitStack=AsyncExitStack,
        json=json, time=time, traceback=traceback, logger=logging.getLogger(__name__),
        TaskResult=Result, Intent=lambda **kw: NS(**kw), AgentEvent=Event,
        ReplyStartEvent=ReplyStart, AgentEventTracer=Tracer,
        UserMsg=lambda **kw: NS(**kw), TextBlock=lambda **kw: NS(**kw),
        AssistantMsg=lambda **kw: NS(**kw), _ToolResultEnd=type("ToolEnd", (), {}),
        serialize_agent_event=lambda ev: '{"type":"delta"}',
        merge_bocha_snapshots=lambda a, b: a + b,
        _is_context_length_error=lambda exc: False,
        _safe_update_span=lambda *args: None,
        load_agent_state=AsyncMock(return_value=None),
    )
    methods("app/utils/agent_event_stream.py", ["iter_agent_events"], env)
    capture = methods("app/services/orchestrator_service.py",
                      ["_capture_tokens_from_stream"], env, "OrchestratorService")
    intent = NS(id="i", agent="a", query="question")
    state = NS(final_states={}, input_tokens=0, output_tokens=0)
    event = lambda self, data: f"data: {json.dumps(data)}\n\n"
    if mode == "direct":
        funcs = methods("app/services/orchestrator_service.py",
                        ["_run_single_agent_path"], env, "OrchestratorService")
        cls = type("Direct", (), dict(funcs, _event=event,
                   _span=lambda self, svc, name, data: svc.start_span(name)))
        obj = cls()
        stream = obj._run_single_agent_path(
            NS(get_definition=lambda aid: True), factory, "a", "question",
            "s", "u", None, spans, state)
    else:
        funcs = methods("app/orchestrator/base.py", ["_run_single_agent"], env, "BaseOrchestrator")
        name = {"pipeline": "PipelineOrchestrator", "parallel": "ParallelOrchestrator",
                "react": "ReActOrchestrator"}[mode]
        selected = ["run", "_execute_action"] if mode == "react" else ["run"]
        funcs.update(methods(f"app/orchestrator/{mode}.py", selected, env, name))
        cls = type(name, (), dict(funcs, _event=event))
        obj = cls()
        obj.agent_factory = factory
        obj._timeout = obj._step_timeout = 0.03
        obj._max_steps = 1
        obj._make_timeout_result = lambda i: Result(i.id, i.agent, False, "timeout")
        obj._build_task_description = obj._build_available_actions = lambda ir: ""
        obj._think = AsyncMock(return_value={
            "next_action": {"name": "call_agent", "args": {"agent_id": "a", "query": "q"}}})
        stream = obj.run(NS(intents=[intent] * intent_count), langfuse_service=spans)
    return capture["_capture_tokens_from_stream"](None, stream, state)


class StreamCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mode, exit_mode, intent_count=1):
        spans = Spans()
        behavior = "blocked" if exit_mode in ("cancel", "timeout") else exit_mode
        source = streams(mode, behavior, spans, intent_count)
        entered = asyncio.Event()

        async def consume():
            with spans.start_span("root"):
                async with aclosing(source):
                    async for chunk in source:
                        if '"type":"delta"' in chunk:
                            entered.set()
                            if exit_mode == "close":
                                break
            self.assertEqual(spans.current.get(), "outside")

        task = asyncio.create_task(consume())
        if exit_mode == "cancel":
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 1)
        self.assertFalse(spans.errors, spans.errors)
        self.assertFalse(spans.active)
        sdk_count = 2 if exit_mode == "error" and mode != "direct" else 1
        self.assertEqual(spans.closed[-1], "root")
        self.assertEqual(sorted(spans.closed[:-1]),
                         sorted((["sdk"] * sdk_count + ["agent-a"]) * intent_count))

    async def test_parallel_stop_waits_for_all_runners(self):
        await self.exercise("parallel", "cancel", intent_count=2)

    async def test_normal_completion_all_paths(self):
        for mode in ("direct", "pipeline", "react", "parallel"):
            with self.subTest(mode=mode):
                await self.exercise(mode, "normal")

    async def test_early_close_at_yield_all_paths(self):
        for mode in ("direct", "pipeline", "react", "parallel"):
            with self.subTest(mode=mode):
                await self.exercise(mode, "close")

    async def test_stop_during_sdk_wait_all_paths(self):
        for mode in ("direct", "pipeline", "react", "parallel"):
            with self.subTest(mode=mode):
                await self.exercise(mode, "cancel")

    async def test_agent_error_all_paths(self):
        for mode in ("direct", "pipeline", "react", "parallel"):
            with self.subTest(mode=mode):
                await self.exercise(mode, "error")

    async def test_step_timeout_closes_children(self):
        for mode in ("pipeline", "parallel"):
            with self.subTest(mode=mode):
                await self.exercise(mode, "timeout")

    async def test_sse_stop_closes_source_in_pump_context(self):
        routes, spans = load_routes(), Spans()
        stopped, entered = asyncio.Event(), asyncio.Event()

        async def source():
            with spans.start_span("source"):
                entered.set()
                yield "first"
                await asyncio.Event().wait()

        async def consume():
            return [ev async for ev in routes["_with_heartbeat"](source(), stopped, None, None)]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 1)
        stopped.set()
        await asyncio.wait_for(task, 1)
        self.assertFalse(spans.active)
        self.assertFalse(spans.errors)
        self.assertEqual(spans.closed, ["source"])


class LeaseRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_and_changed_owner_are_lost_without_revival(self):
        for code, reason in ((-1, "key_missing"), (-2, "owner_mismatch"), (0, "renew_rejected")):
            with self.subTest(code=code):
                redis = MemoryRedis()
                lease = RedisLease(redis, "chat_run:s", ttl=1, token="original-owner")
                await lease.acquire()
                lease._renew_task.cancel()
                await asyncio.gather(lease._renew_task, return_exceptions=True)
                if code == -1:
                    await redis.delete(lease.key)
                else:
                    await redis.set(lease.key, "new-owner")
                redis.eval = AsyncMock(return_value=code)
                lease._renew_at = time.monotonic()
                with self.assertLogs("app.services.redis_lease", logging.WARNING) as logs:
                    await lease._renew()
                self.assertTrue(lease.lost.is_set())
                self.assertFalse(lease.locally_valid())
                self.assertIn(f"reason={reason}", "\n".join(logs.output))
                self.assertNotIn("original-owner", "\n".join(logs.output))
                with self.assertRaises(LeaseLost):
                    await lease.assert_owned()
                self.assertEqual(await redis.get(lease.key), None if code == -1 else "new-owner")
                await lease.release()

    async def test_nx_and_release_preserve_other_owner(self):
        redis = MemoryRedis()
        first = RedisLease(redis, "chat_run:s")
        second = RedisLease(redis, "chat_run:s")
        await first.acquire()
        try:
            self.assertFalse(await second.acquire())
            self.assertEqual(await redis.get(first.key), first.token)
            await redis.set(first.key, second.token)
            await first.release()
            self.assertEqual(await redis.get(first.key), second.token)
        finally:
            await first.release()


class DetachFilterTests(unittest.TestCase):
    def test_only_known_cross_context_detach_is_filtered(self):
        tree = ast.parse((ROOT / "app/utils/logging_setup.py").read_text(encoding="utf-8-sig"))
        node = next(n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "CrossContextDetachFilter")
        env = {"logging": logging}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "logging_setup.py", "exec"), env)
        filt = env["CrossContextDetachFilter"]()
        for name, message, exc, expected in (
            ("opentelemetry.context", "Failed to detach context",
             ValueError("Token was created in a different Context"), False),
            ("opentelemetry.context", "Failed to detach context", ValueError("other"), True),
            ("opentelemetry.context", "Failed to detach context", RuntimeError("other"), True),
            ("app", "Failed to detach context",
             ValueError("Token was created in a different Context"), True),
            ("opentelemetry.context", "different error",
             ValueError("Token was created in a different Context"), True),
        ):
            record = logging.LogRecord(name, logging.ERROR, "", 1, message, (),
                                       (type(exc), exc, None))
            self.assertEqual(filt.filter(record), expected)

    def test_span_exit_preserves_unrelated_value_error(self):
        env = dict(sys=sys, logger=logging.getLogger(__name__))
        funcs = methods("app/services/langfuse_service.py", ["start_span"],
                        env, "LangfuseService")
        start_span = contextmanager(funcs["start_span"])
        for message in ("invalid span output", "Token was created in a different Context"):
            cm = NS(__enter__=lambda: object(),
                    __exit__=lambda *args: (_ for _ in ()).throw(ValueError(message)))
            service = NS(_enabled=True,
                         _client=NS(start_as_current_observation=lambda **kw: cm))
            if message == "invalid span output":
                with self.assertRaisesRegex(ValueError, message):
                    with start_span(service, "test"):
                        pass
            else:
                with self.assertLogs(__name__, logging.WARNING):
                    with start_span(service, "test"):
                        pass
