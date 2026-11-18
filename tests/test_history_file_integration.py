"""会话工具装配及恢复文件的消息归属回归。"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.round_state import RoundState


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_restored_files_only_emitted_if_modified(monkeypatch, changed):
    import app.services.chat_service as chat
    monkeypatch.setattr(chat, "SESSION_FILES_PERSIST_DIR", "")
    original = ("100.0000000000", "4")
    after = {"old.pptx": ("101.0000000000", "8") if changed else original,
             "new-v2.pptx": ("200.0000000000", "8")}
    mgr = SimpleNamespace(list_session_files=AsyncMock(return_value=after),
                          stat_session_file=AsyncMock(return_value=8))
    events = [event async for event in chat._detect_and_emit_files(
        "session-1", {}, None, workspace_manager=mgr, user_id="u1",
        restored_files={"old.pptx": original},
    )]
    files = json.loads(events[0][6:])["files"]
    paths = {f["path"] for f in files}
    assert paths == ({"old.pptx", "new-v2.pptx"} if changed else {"new-v2.pptx"})


@pytest.mark.asyncio
async def test_workspace_registers_session_bound_history_tools(tmp_path, monkeypatch):
    import app.services.workspace_assembler as assembly
    from agentscope.skill import Skill
    sandbox = SimpleNamespace(id="new-sandbox", workdir="/data/workspaces/session-1")
    mgr = SimpleNamespace(get_workspace=AsyncMock(return_value=sandbox), list_skills=AsyncMock(return_value=[
        Skill(name="pptx", description="slides", dir="/workspace/skills/pptx", markdown="", updated_at=0),
    ]))
    session = SimpleNamespace(dao=SimpleNamespace(get_session_meta=AsyncMock(return_value={"user_id": "u1"})),
                              load_session_files=AsyncMock(return_value=[]))
    rs = RoundState()
    registry, _ = await assembly.assemble_workspace_components(
        mgr, lambda: None,
        {"merged_agents": [{"id": "slides", "name": "slides", "skills": ["pptx"]}], "merged_skills": []},
        "u1", None, "u1", "session-1", search_enabled=False,
        session_service=session, round_state=rs,
    )
    tools = {tool.name: tool for tool in registry._all_tools}
    assert {"ListHistoryFiles", "RestoreHistoryFile"} <= tools.keys()
    chunk = await tools['ListHistoryFiles']()
    assert json.loads(chunk.content[0].text)['files'] == []
    session.dao.get_session_meta.assert_awaited_with("session-1")


@pytest.mark.asyncio
async def test_restore_tool_uses_current_sandbox_and_records_round_signature(tmp_path, monkeypatch):
    import app.config as config
    from app.services.workspace_assembler import assemble_workspace_components
    monkeypatch.setattr(config, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    data = b"PK\x03\x04\xff\x00"
    backup = tmp_path / 'session-1' / 'old.pptx'
    backup.parent.mkdir()
    backup.write_bytes(data)
    def execution(stdout=''):
        return SimpleNamespace(exit_code=0, logs=SimpleNamespace(
            stdout=[SimpleNamespace(text=stdout)] if stdout else [], stderr=[]))
    sandbox = SimpleNamespace(
        id='fresh-sandbox', commands=SimpleNamespace(run=AsyncMock(side_effect=[
            execution(), execution('restored\n100.0000000000 6'), execution(),
        ])), files=SimpleNamespace(write_files=AsyncMock()),
    )
    mgr = SimpleNamespace(get_workspace=AsyncMock(return_value=sandbox),
                          list_skills=AsyncMock(return_value=[]))
    session = SimpleNamespace(
        dao=SimpleNamespace(get_session_meta=AsyncMock(return_value={'user_id': 'u1'})),
        load_session_files=AsyncMock(return_value=[{
            'name': 'old.pptx', 'path': 'old.pptx', 'size': len(data),
            'url': '/persist-files/session-1/old.pptx',
        }]),
    )
    rs = RoundState()
    registry, _ = await assemble_workspace_components(
        mgr, lambda: None, {'merged_agents': [], 'merged_skills': []},
        'u1', None, 'u1', 'session-1', search_enabled=False,
        session_service=session, round_state=rs,
    )
    tool = next(t for t in registry._all_tools if t.name == 'RestoreHistoryFile')
    result = json.loads((await tool(file_path='old.pptx')).content[0].text)
    assert result['status'] == 'restored'
    assert result['path'] == '/data/workspaces/session-1/old.pptx'
    assert sandbox.files.write_files.call_args.args[0][0].data == data
    assert rs.restored_files == {'old.pptx': ('100.0000000000', '6')}


@pytest.mark.asyncio
async def test_restore_tool_reports_unavailable_instead_of_claiming_success(tmp_path):
    from tools.history_file_tools import create_history_file_tools
    svc = SimpleNamespace(session_id="session-1", list_files=AsyncMock(return_value={"files": []}),
                          restore_file=AsyncMock(side_effect=OSError("private backend disk path")))
    tool = create_history_file_tools(svc)[1]
    chunk = await tool(file_path="old.pptx")
    result = json.loads(chunk.content[0].text)
    assert result['status'] == 'error'
    assert "private backend disk path" not in result['message']


@pytest.mark.asyncio
async def test_history_tool_returns_safe_error_for_unserializable_metadata():
    from datetime import datetime
    from tools.history_file_tools import create_history_file_tools
    svc = SimpleNamespace(session_id="s", list_files=AsyncMock(return_value={"created_at": datetime.now()}))
    chunk = await create_history_file_tools(svc)[0]()
    assert json.loads(chunk.content[0].text)["status"] == "error"
