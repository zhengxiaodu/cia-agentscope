"""Exercise production file IO and probe handlers without optional app SDKs."""
import ast
import asyncio
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid
import zipfile

from app.services.file_change_detector import diff, filter_allowed
from tests.test_workspace_redis_map import ws, manager

ROOT = Path(__file__).resolve().parents[1]


def load_file_functions():
    tree = ast.parse((ROOT / "app/services/chat_service.py").read_text(encoding="utf-8-sig"))
    names = {"_persist_file_bytes", "_persist_file_bytes_async", "_finished_artifact", "_detect_and_emit_files"}
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes.extend(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names)
    env = dict(asyncio=asyncio, os=os, uuid=uuid, io=io, zipfile=zipfile, json=json,
               logger=logging.getLogger(__name__), diff=diff, filter_allowed=filter_allowed,
               SESSION_FILES_PERSIST_DIR="")
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "chat_files.py", "exec"), env)
    return env


class FileIOTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_archive_waits_for_worker_before_round_cleanup(self):
        env = load_file_functions()
        loop, started, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
        original = env["_persist_file_bytes"]
        def slow_write(*args):
            loop.call_soon_threadsafe(started.set)
            release.wait(1)
            return original(*args)
        env["_persist_file_bytes"] = slow_write
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            task = asyncio.create_task(env["_persist_file_bytes_async"](
                directory, "session", "result.md", b"complete"))
            try:
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertEqual(Path(directory, "session/result.md").read_bytes(), b"complete")

    async def test_archive_write_allows_event_loop_progress_and_preserves_bytes(self):
        env = load_file_functions()
        loop, started, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
        original = env["_persist_file_bytes"]
        content = b"binary\x00content\xff"

        def slow_write(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(0.5):
                raise RuntimeError("archive IO blocked the event loop")
            return original(*args)

        env["_persist_file_bytes"] = slow_write
        workspace = SimpleNamespace(
            list_session_files=AsyncMock(return_value={"reports/result.md": ("1", str(len(content)))}),
            stat_session_file=AsyncMock(return_value=len(content)),
            read_session_file=AsyncMock(return_value=content),
        )
        service = SimpleNamespace(append_session_files=AsyncMock())
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            env["SESSION_FILES_PERSIST_DIR"] = directory
            async def collect():
                return [item async for item in env["_detect_and_emit_files"](
                    "session", {}, service, workspace, "user", "pair", 123)]
            task = asyncio.create_task(collect())
            try:
                await asyncio.wait_for(started.wait(), 1)
                self.assertFalse(task.done())
            finally:
                release.set()
            output = await asyncio.wait_for(task, 1)
            self.assertEqual(Path(directory, "session/reports/result.md").read_bytes(), content)
            payload = json.loads(output[0].split("data: ", 1)[1])
            item = payload["files"][0]
            self.assertEqual(item["url"], "/persist-files/session/reports/result.md")
            self.assertEqual((item["message_id"], item["message_pair_id"]), (123, "pair"))
            service.append_session_files.assert_awaited_once_with("session", payload["files"])

    async def test_archive_failure_preserves_download_fallback_and_path_checks(self):
        env = load_file_functions()
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            env["SESSION_FILES_PERSIST_DIR"] = directory
            self.assertFalse(env["_persist_file_bytes"](directory, "session", "../other.md", b"x"))
            workspace = SimpleNamespace(
                list_session_files=AsyncMock(return_value={"result.md": ("1", "1")}),
                stat_session_file=AsyncMock(return_value=1),
                read_session_file=AsyncMock(return_value=b"x"),
            )
            with patch.object(os, "replace", side_effect=OSError("disk unavailable")):
                output = [item async for item in env["_detect_and_emit_files"](
                    "session", {}, None, workspace)]
            payload = json.loads(output[0].split("data: ", 1)[1])
            self.assertEqual(payload["files"][0]["url"], "/files/session/result.md")
            self.assertEqual(list(Path(directory).rglob(".archive-*")), [])

    async def test_skill_reads_allow_event_loop_progress_and_keep_nested_binary_files(self):
        mgr = manager()
        loop, started, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
        original = mgr._collect_skill_entries
        def slow_read(directories):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(0.5):
                raise RuntimeError("skill reads blocked the event loop")
            return original(directories)
        sandbox = SimpleNamespace(files=SimpleNamespace(write_files=AsyncMock()))
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            skill = Path(directory, "example")
            skill.mkdir()
            (skill / "nested").mkdir()
            (skill / "nested" / "data.bin").write_bytes(b"\x00\xff")
            (skill / "SKILL.md").write_text("技能", encoding="utf-8")
            with patch.object(mgr, "_collect_skill_entries", slow_read), \
                 patch.object(ws, "WriteEntry", SimpleNamespace):
                task = asyncio.create_task(mgr._inject_skills(sandbox, [str(skill)]))
                try:
                    await asyncio.wait_for(started.wait(), 1)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                await asyncio.wait_for(task, 1)
            entries = sandbox.files.write_files.await_args.args[0]
            files = {entry.path.replace("\\", "/"): entry.data for entry in entries}
            self.assertEqual(files["/workspace/skills/example/nested/data.bin"], b"\x00\xff")
            self.assertEqual(files["/workspace/skills/example/SKILL.md"].decode("utf-8"), "技能")

    async def test_health_and_ping_register_the_same_lightweight_handler(self):
        tree = ast.parse((ROOT / "app/routes/health.py").read_text(encoding="utf-8-sig"))
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "health")
        registered = {}
        def get(path):
            def register(handler):
                registered[path] = handler
                return handler
            return register
        module = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            function,
        ], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "health.py", "exec"),
             {"router": SimpleNamespace(get=get)})
        self.assertIs(registered["/health"], registered["/ping"])
        self.assertEqual(await registered["/health"](None), "SUCCESSED")


if __name__ == "__main__":
    unittest.main()
