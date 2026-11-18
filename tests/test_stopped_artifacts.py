import asyncio
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.opensandbox_adapter import OpenSandboxToolAdapter
from app.services.opensandbox_tool_bridge import create_opensandbox_tools
from app.services.sandbox_file_checkpoint import SandboxFileCheckpoint


@pytest.mark.asyncio
async def test_write_and_edit_keep_nested_targets_and_refuse_escape():
    from unittest.mock import MagicMock
    adapter = OpenSandboxToolAdapter(MagicMock(), workdir="/data/workspaces/s")
    adapter.bash = AsyncMock(return_value={"exit_code": 0})
    adapter.write = AsyncMock()
    adapter.edit = AsyncMock()
    tools = {tool.name: tool for tool in create_opensandbox_tools(adapter)}
    await tools["Write"](file_path="reports/a b.md", content="before")
    await tools["Edit"](file_path="/data/workspaces/s/reports/a b.md", old_text="before", new_text="after")
    adapter.write.assert_awaited_once_with("/data/workspaces/s/reports/a b.md", "before")
    adapter.edit.assert_awaited_once_with("/data/workspaces/s/reports/a b.md", "before", "after")
    for path in ("/data/workspaces/other/note.md", "../other/note.md"):
        chunk = await tools["Write"](file_path=path, content="x")
        assert "Error" in chunk.content[0].text
    adapter.bash.return_value = {"exit_code": 73}
    await tools["Edit"](file_path="symlink/note.md", old_text="a", new_text="b")
    assert adapter.write.await_count == adapter.edit.await_count == 1


@pytest.mark.asyncio
async def test_failed_and_overlapping_writes_do_not_certify_partial_files():
    completed = {}
    earlier = {"done.md": ("1", "10")}
    snapshot = AsyncMock(side_effect=[{}, earlier, earlier])
    tracker = SandboxFileCheckpoint(snapshot, completed)
    await tracker.run(AsyncMock(return_value={"exit_code": 0}))
    started, release = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await release.wait()
        return {"exit_code": 0}

    worker = asyncio.create_task(tracker.run(slow))
    await started.wait()
    await tracker.run(AsyncMock(return_value={"exit_code": 1}))
    release.set()
    await worker
    assert completed == earlier
    assert snapshot.await_count == 3


def pptx_bytes():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("ppt/presentation.xml", "<presentation/>")
    return out.getvalue()


@pytest.mark.asyncio
async def test_stop_persists_only_completed_stable_artifacts(tmp_path, monkeypatch):
    from app.routes.chat import _with_heartbeat
    from app.services import chat_service as chat
    from tests.test_chat_stop_redis_flag import _make_orch, _make_request, _make_session_service, _patch_sensitive

    _patch_sensitive(monkeypatch)
    monkeypatch.setattr(chat, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    data = pptx_bytes()
    content = {"old.md": b"old", "reports/done.pptx": data, "broken.pptx": data[:-40],
               "in-progress.md": b"pending", "changed.md": b"changed"}
    signatures = {path: ("2", str(len(value))) for path, value in content.items()}
    confirmed = dict(signatures)
    confirmed.pop("in-progress.md")
    verified = {**signatures, "changed.md": ("3", "7")}
    manager = SimpleNamespace(
        list_session_files=AsyncMock(side_effect=[{}, signatures, verified, verified]),
        stat_session_file=AsyncMock(side_effect=lambda u,s,p: len(content[p])),
        read_session_file=AsyncMock(side_effect=lambda u,s,p: content[p]),
    )
    session = _make_session_service()
    stop = asyncio.Event()
    orch = _make_orch([])

    async def run(*args, round_state, **kwargs):
        round_state.restored_files["old.md"] = signatures["old.md"]
        tracker = SandboxFileCheckpoint(AsyncMock(side_effect=[{}, confirmed]), round_state.completed_files)
        await tracker.run(AsyncMock(return_value={"exit_code": 0}))
        stop.set()
        await asyncio.Event().wait()
        yield ""

    orch.run = run
    source = chat.generate_response(orch, [{"role":"user","content":"生成文件"}], session_id="s", user_id="u",
                                    request=_make_request(), session_service=session, workspace_manager=manager,
                                    cancel_event=stop)
    events = [json.loads(e[6:]) async for e in _with_heartbeat(source, stop) if e.startswith("data: ")]
    files = next(e["files"] for e in events if e["type"] == "files_generated")
    assert [f["path"] for f in files] == ["reports/done.pptx"]
    assert files[0]["message_id"] == 102 and files[0]["message_pair_id"]
    assert (tmp_path / "s/reports/done.pptx").read_bytes() == data
    assert events[-1]["type"] == "user_abort"
    # 用户主动停止（本地 event 置位）→ 归因 user，文案保持“用户已停止”
    assert events[-1]["reason"] == "user"
    assert events[-1]["message"] == "用户已停止"
    session.append_session_files.assert_awaited_once_with("s", files)
    session.append_messages.assert_awaited_once()
    assert session.append_messages.await_args.args[2][-1]["success"] is False

    from app.services.historical_file_service import HistoricalFileService
    session.dao.get_session_meta = AsyncMock(return_value={"user_id": "u"})
    session.load_session_files = AsyncMock(return_value=files)
    fresh = SimpleNamespace(workdir="/data/workspaces/s", file_signature=AsyncMock(return_value=None),
                            restore_file_if_missing=AsyncMock(return_value=(True, signatures["reports/done.pptx"])))
    restored = await HistoricalFileService(session, fresh, "u", "s", tmp_path).restore_file("done.pptx")
    assert restored["status"] == "restored"
    fresh.restore_file_if_missing.assert_awaited_once_with("reports/done.pptx", data)

@pytest.mark.asyncio
async def test_real_bash_snapshot_and_guard(tmp_path):
    import os
    import shutil
    import subprocess
    from pathlib import Path

    bash = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("requires Bash with GNU find, as in the sandbox image")

    def shell(cmd):
        return subprocess.run([str(bash), "-c", "export PATH=/usr/bin:$PATH\n" + cmd],
                              cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    cwd_result = shell("pwd -P")
    cwd_result.check_returncode()
    workdir = cwd_result.stdout.strip()
    adapter = OpenSandboxToolAdapter(None, workdir)
    async def execute(cmd, timeout=30):
        res = shell(cmd)
        return {"exit_code": res.returncode, "stdout": res.stdout, "stderr": res.stderr}
    adapter.bash = execute
    rel = "reports/1 'a $b.md"
    target = await adapter.workspace_file_target(rel, create_parent=True)
    assert target == f"{workdir}/{rel}"
    (tmp_path / rel).write_bytes(b"complete")
    (tmp_path / "draft.log").write_bytes(b"temp")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads/old.md").write_bytes(b"old")
    signatures = await adapter.artifact_signatures()
    assert list(signatures) == [rel]
    assert signatures[rel] == await adapter.file_signature(rel)

@pytest.mark.asyncio
async def test_second_stop_during_history_commit_archives_once(monkeypatch, tmp_path):
    from app.services import chat_service as chat
    from tests.test_chat_stop_redis_flag import _make_orch, _make_request, _make_session_service, _patch_sensitive
    _patch_sensitive(monkeypatch)
    monkeypatch.setattr(chat, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    signature = {"done.md": ("2", "2")}
    manager = SimpleNamespace(list_session_files=AsyncMock(side_effect=[{}, signature, signature]),
                              stat_session_file=AsyncMock(return_value=2),
                              read_session_file=AsyncMock(return_value=b"ok"))
    session = _make_session_service()
    saving, release = asyncio.Event(), asyncio.Event()
    async def save(*args):
        saving.set()
        await release.wait()
        return {"user_message_id": 1, "assistant_message_id": 2}
    session.append_messages.side_effect = save
    orch = _make_orch([])
    async def run(*args, round_state, **kwargs):
        round_state.completed_files.update(signature)
        raise asyncio.CancelledError()
        yield ""
    orch.run = run
    async def collect():
        return [json.loads(ev[6:]) async for ev in chat.generate_response(
            orch, [{"role": "user", "content": "生成文件"}], session_id="s", user_id="u",
            session_service=session, request=_make_request(), workspace_manager=manager)]
    pump = asyncio.create_task(collect())
    await asyncio.wait_for(saving.wait(), timeout=5)
    pump.cancel()
    release.set()
    events = await asyncio.wait_for(pump, timeout=10)
    assert [e["type"] for e in events].count("files_generated") == 1
    assert events[-1]["type"] == "user_abort"
    # 无 cancel_event 的外部取消（客户端断连/请求被取消）→ 归因 client_disconnect
    assert events[-1]["reason"] == "client_disconnect"
    assert events[-1]["message"] == "会话异常中断，请重试"
    session.append_messages.assert_awaited_once()
    session.append_session_files.assert_awaited_once()
