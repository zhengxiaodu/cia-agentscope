"""OpenSandbox 边界：原始二进制、原子不覆盖、错误与工作目录传递。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.opensandbox_adapter import OpenSandboxToolAdapter


def execution(stdout="", code=0):
    return SimpleNamespace(exit_code=code, logs=SimpleNamespace(
        stdout=[SimpleNamespace(text=stdout)] if stdout else [], stderr=[]))


def adapter_with(results):
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=AsyncMock(side_effect=results)),
                              files=SimpleNamespace(write_files=AsyncMock()))
    return OpenSandboxToolAdapter(sandbox, "/data/workspaces/session-1"), sandbox


@pytest.mark.asyncio
async def test_bash_passes_session_workdir_and_timeout_to_sdk():
    adapter, sandbox = adapter_with([execution("ok")])
    assert (await adapter.bash("pwd", timeout=7))["stdout"] == "ok"
    opts = sandbox.commands.run.call_args.kwargs["opts"]
    assert opts.working_directory == adapter.workdir
    assert opts.timeout.total_seconds() == 7


@pytest.mark.asyncio
async def test_restore_preserves_binary_bytes_and_uses_atomic_non_overwriting_publish():
    adapter, sandbox = adapter_with([execution("restored\n100.0000000000 4"), execution()])
    data = b"PK\x00\xff"
    restored, signature = await adapter.restore_file_if_missing("reports/a.pptx", data)
    assert restored and signature == ("100.0000000000", "4")
    entry = sandbox.files.write_files.call_args.args[0][0]
    assert entry.data == data and isinstance(entry.data, bytes)
    assert entry.mode == 600  # Execd API 接收十进制承载的八进制数字，而非 POSIX 位掩码。
    assert entry.path.startswith(adapter.workdir + "/.history-restore-")
    command = sandbox.commands.run.call_args_list[0].args[0]
    assert "ln -T --" in command and "realpath" in command
    assert "rm -f --" in sandbox.commands.run.call_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_restore_race_does_not_report_existing_file_as_restored():
    adapter, sandbox = adapter_with([execution("exists\n101.0000000000 8"), execution()])
    assert await adapter.restore_file_if_missing("a.pptx", b"original") == (
        False, ("101.0000000000", "8"))


@pytest.mark.asyncio
async def test_restore_commit_failure_cleans_staging_file_and_raises():
    adapter, sandbox = adapter_with([execution("", code=73), execution()])
    with pytest.raises(RuntimeError):
        await adapter.restore_file_if_missing("a.pptx", b"PK")
    assert "rm -f --" in sandbox.commands.run.call_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_upload_failure_also_cleans_partial_staging_file():
    adapter, sandbox = adapter_with([execution()])
    sandbox.files.write_files.side_effect = OSError("upload interrupted")
    with pytest.raises(OSError):
        await adapter.restore_file_if_missing("a.pptx", b"PK")
    assert "rm -f --" in sandbox.commands.run.call_args.args[0]


@pytest.mark.asyncio
async def test_file_signature_distinguishes_missing_from_remote_failure():
    adapter, _ = adapter_with([execution(), execution(code=74)])
    assert await adapter.file_signature("a.pptx") is None
    with pytest.raises(RuntimeError):
        await adapter.file_signature("a.pptx")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside.pptx", "/tmp/a.pptx", "a/../b.pptx", "a\\..\\b", "C:/a.pptx"])
async def test_binary_restore_rejects_escaping_destination(path):
    adapter, sandbox = adapter_with([])
    with pytest.raises(ValueError):
        await adapter.restore_file_if_missing(path, b"PK")
    sandbox.files.write_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_shell_metacharacters_in_filenames_are_quoted():
    adapter, sandbox = adapter_with([execution("restored\n100.0000000000 2"), execution()])
    await adapter.restore_file_if_missing("folder/a'$(touch nope).pptx", b"PK")
    command = sandbox.commands.run.call_args_list[0].args[0]
    assert "'\"'\"'" in command
