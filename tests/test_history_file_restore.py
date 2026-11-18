"""历史产物恢复：真实临时备份文件，远程沙箱只用文件系统边界替身。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.round_state import RoundState


PPTX = b"PK\x03\x04\x00\xffbinary-pptx\x00\x80"


class SandboxFiles:
    def __init__(self):
        self.workdir = "/data/workspaces/session-1"
        self.files = {}
        self.writes = []

    async def file_signature(self, path):
        if path not in self.files:
            return None
        return ("100.0000000000", str(len(self.files[path])))

    async def restore_file_if_missing(self, path, content):
        if path in self.files:
            return False, await self.file_signature(path)
        self.files[path] = content
        self.writes.append(path)
        return True, await self.file_signature(path)


def make_service(tmp_path, rows=None, owner="u1", adapter=None, restored=None):
    from app.services.historical_file_service import HistoricalFileService
    rows = rows if rows is not None else [{
        "name": "报告.pptx", "path": "reports/报告.pptx", "size": len(PPTX),
        "url": "/persist-files/session-1/reports/报告.pptx", "message_pair_id": "old-pair",
    }]
    session = SimpleNamespace(
        dao=SimpleNamespace(get_session_meta=AsyncMock(return_value={"user_id": owner})),
        load_session_files=AsyncMock(return_value=rows),
    )
    remote = adapter or SandboxFiles()
    service = HistoricalFileService(session, remote, "u1", "session-1", tmp_path,
                                    restored_files=restored if restored is not None else {})
    return service, session, remote


def write_backup(tmp_path, rel="reports/报告.pptx", content=PPTX):
    path = tmp_path / "session-1" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.asyncio
async def test_restore_pptx_bytes_on_fresh_workspace(tmp_path):
    write_backup(tmp_path)
    baseline = {}
    service, session, remote = make_service(tmp_path, restored=baseline)
    result = await service.restore_file("reports/报告.pptx")
    assert result["status"] == "restored"
    assert result["path"] == "/data/workspaces/session-1/reports/报告.pptx"
    assert remote.files["reports/报告.pptx"] == PPTX
    assert baseline["reports/报告.pptx"] == ("100.0000000000", str(len(PPTX)))
    session.dao.get_session_meta.assert_awaited_once_with("session-1")


@pytest.mark.asyncio
async def test_existing_work_file_is_not_overwritten_even_if_backup_unavailable(tmp_path):
    service, _, remote = make_service(tmp_path)
    remote.files["reports/报告.pptx"] = b"current edits"
    assert (await service.restore_file("reports/报告.pptx"))["status"] == "already_present"
    assert remote.files["reports/报告.pptx"] == b"current edits"
    assert not remote.writes


@pytest.mark.asyncio
async def test_repeated_restore_and_shared_backup_across_replicas(tmp_path):
    write_backup(tmp_path)
    first, _, one = make_service(tmp_path)
    second, _, two = make_service(tmp_path)
    assert (await first.restore_file("报告.pptx"))["status"] == "restored"
    assert (await first.restore_file("报告.pptx"))["status"] == "already_present"
    assert (await second.restore_file("报告.pptx"))["status"] == "restored"
    assert one.files == two.files
    assert len(one.writes) == 1


@pytest.mark.asyncio
async def test_permission_checked_before_listing_or_reading(tmp_path):
    service, session, remote = make_service(tmp_path, owner="another-user")
    with pytest.raises(PermissionError):
        await service.restore_file("reports/报告.pptx")
    session.load_session_files.assert_not_awaited()
    assert not remote.writes


@pytest.mark.asyncio
async def test_history_database_error_is_not_empty_history(tmp_path):
    service, session, _ = make_service(tmp_path)
    session.load_session_files.side_effect = OSError("database unavailable")
    with pytest.raises(OSError):
        await service.list_files()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../other/file.pptx", "/tmp/file.pptx", "C:/file.pptx", "a/../../file", "a\\..\\file", "bad\x00name"])
async def test_restore_rejects_paths_outside_session(tmp_path, path):
    service, _, remote = make_service(tmp_path)
    with pytest.raises(ValueError):
        await service.restore_file(path)
    assert not remote.writes


@pytest.mark.asyncio
async def test_metadata_is_required_and_duplicate_basename_is_ambiguous(tmp_path):
    rows = [{"name": "a.pptx", "path": p, "url": f"/persist-files/session-1/{p}"}
            for p in ["one/a.pptx", "two/a.pptx"]]
    service, _, _ = make_service(tmp_path, rows=rows)
    with pytest.raises(ValueError, match="多个"):
        await service.restore_file("a.pptx")
    with pytest.raises(ValueError):
        await service.restore_file("unregistered.pptx")


@pytest.mark.asyncio
async def test_missing_backup_and_size_mismatch_are_clear_failures(tmp_path):
    service, _, remote = make_service(tmp_path)
    with pytest.raises(ValueError, match="备份"):
        await service.restore_file("reports/报告.pptx")
    write_backup(tmp_path, content=b"truncated")
    with pytest.raises(ValueError, match="大小"):
        await service.restore_file("reports/报告.pptx")
    assert not remote.writes


@pytest.mark.asyncio
async def test_unbacked_sandbox_only_file_does_not_restore_stale_copy(tmp_path):
    write_backup(tmp_path)
    service, _, remote = make_service(tmp_path, rows=[{
        "name": "报告.pptx", "path": "reports/报告.pptx", "size": len(PPTX),
        "url": "/files/session-1/reports/报告.pptx",
    }])
    with pytest.raises(ValueError, match="备份"):
        await service.restore_file("reports/报告.pptx")
    assert not remote.writes


@pytest.mark.asyncio
async def test_list_only_loads_metadata_and_keeps_paths_distinct(tmp_path):
    service, _, remote = make_service(tmp_path)
    listing = await service.list_files()
    assert listing["files"][0]["path"] == "reports/报告.pptx"
    assert listing["files"][0]["message_pair_id"] == "old-pair"
    assert not remote.writes
    assert (await service.list_files(query="不存在"))["files"] == []


@pytest.mark.asyncio
async def test_concurrent_restore_publishes_once(tmp_path):
    write_backup(tmp_path)
    service, _, remote = make_service(tmp_path)
    results = await asyncio.gather(*(service.restore_file("报告.pptx") for _ in range(3)))
    assert sum(r["status"] == "restored" for r in results) == 1
    assert len(remote.writes) == 1


@pytest.mark.asyncio
async def test_binary_restoration_failure_propagates(tmp_path):
    write_backup(tmp_path)
    service, _, remote = make_service(tmp_path)
    remote.restore_file_if_missing = AsyncMock(side_effect=OSError("sandbox offline"))
    with pytest.raises(OSError):
        await service.restore_file("报告.pptx")


def test_restoration_bookkeeping_is_request_scoped():
    one, two = RoundState(), RoundState()
    one.restored_files["old.pptx"] = ("100", "20")
    assert two.restored_files == {}


@pytest.mark.asyncio
async def test_symlinked_backup_cannot_escape_session(tmp_path):
    outside = tmp_path / 'another-session' / 'private.pptx'
    outside.parent.mkdir()
    outside.write_bytes(PPTX)
    target = tmp_path / 'session-1' / 'reports' / '报告.pptx'
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip('This Windows account cannot create symlinks')
    service, _, remote = make_service(tmp_path)
    with pytest.raises(ValueError, match='当前会话'):
        await service.restore_file('reports/报告.pptx')
    assert not remote.writes


@pytest.mark.asyncio
async def test_deleted_or_missing_session_cannot_list_history(tmp_path):
    service, session, _ = make_service(tmp_path)
    session.dao.get_session_meta.return_value = None
    with pytest.raises(PermissionError):
        await service.list_files()
    session.load_session_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_literal_percent_filename_can_be_restored(tmp_path):
    rel = '增长%20.pptx'
    write_backup(tmp_path, rel=rel)
    service, _, remote = make_service(tmp_path, rows=[{
        'name': rel, 'path': rel, 'size': len(PPTX), 'url': f'/persist-files/session-1/{rel}',
    }])
    assert (await service.restore_file(rel))['status'] == 'restored'
    assert remote.files[rel] == PPTX


@pytest.mark.asyncio
async def test_disk_permission_error_does_not_expose_host_path(tmp_path, monkeypatch):
    from tools.history_file_tools import create_history_file_tools
    service, _, _ = make_service(tmp_path)
    def unreadable(_):
        raise PermissionError('private-backup-root/other-storage')
    monkeypatch.setattr(Path, 'read_bytes', unreadable)
    result = await create_history_file_tools(service)[1](file_path='reports/报告.pptx')
    import json
    payload = json.loads(result.content[0].text)
    assert payload['status'] == 'error'
    assert 'private-backup-root' not in payload['message']
