"""get_session_detail 扩展测试：upload_files 字段透出与降级语义。

覆盖：
- upload_files 字段结构（name/size/media_type/created_at/message_id/
  message_pair_id）与未消费记录的 null 语义
- messages / files 新字段（message_pair_id / citations / message_id）透出
- upload_file_dao=None 时 upload_files 为 []
- list_files_by_session 抛异常时降级为 [] 且不向外抛出
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.session_service import SessionService


def _make_dao():
    dao = MagicMock()
    dao.get_session_meta = AsyncMock(return_value={
        "session_id": "s1", "user_id": "u1", "name": "会话名",
        "created_at": "2026-01-01 00:00:00.000",
        "updated_at": "2026-01-01 00:00:01.000",
        "message_count": 2, "latest_trace_id": "trace-1",
        "is_pinned": False, "agent_ids": [],
    })
    dao.load_messages = AsyncMock(return_value=[
        {"role": "user", "content": "问", "timestamp": "2026-01-01 00:00:00.100",
         "agent_ids": [], "user_id": "u1", "success": True, "tokens": 3,
         "message_pair_id": "pair-1", "citations": []},
        {"role": "assistant", "content": "答", "timestamp": "2026-01-01 00:00:00.200",
         "agent_ids": ["a1"], "user_id": "u1", "success": True, "tokens": 5,
         "message_pair_id": "pair-1", "citations": [{"title": "差旅办法", "url": "http://x"}]},
    ])
    dao.load_session_files = AsyncMock(return_value=[
        {"name": "a.md", "path": "a.md", "url": "/persist-files/s1/a.md",
         "size": 12, "media_type": "text/markdown",
         "created_at": "2026-01-01 00:00:02.000",
         "message_id": 102, "message_pair_id": "pair-1"},
    ])
    return dao


# 两条上传记录：一条已绑定（被某轮问答消费），一条未消费（null 语义）
_UPLOAD_ROWS = [
    {"name": "报告.pdf", "size": 2048, "media_type": "application/pdf",
     "created_at": "2026-01-01 00:00:00.500",
     "message_id": 101, "message_pair_id": "pair-1"},
    {"name": "语音.m4a", "size": 1024, "media_type": "audio/mp4",
     "created_at": "2026-01-01 00:00:01.500",
     "message_id": None, "message_pair_id": None},
]


def _make_upload_dao(rows=None, exc=None):
    dao = MagicMock()
    dao.list_files_by_session = AsyncMock(
        return_value=rows if rows is not None else _UPLOAD_ROWS
    )
    if exc is not None:
        dao.list_files_by_session = AsyncMock(side_effect=exc)
    return dao


@pytest.mark.asyncio
async def test_get_session_detail_returns_upload_files():
    """upload_files 列出该会话全部上传文件，未消费记录 message_id/pair_id 为 null。"""
    dao = _make_dao()
    upload_dao = _make_upload_dao()
    svc = SessionService(dao, upload_file_dao=upload_dao)

    detail = await svc.get_session_detail("s1", "u1")

    upload_dao.list_files_by_session.assert_awaited_once_with("s1")
    dumped = detail.model_dump()
    uploads = dumped["upload_files"]
    assert len(uploads) == 2
    # 契约字段齐全
    assert set(uploads[0].keys()) == {
        "name", "size", "media_type", "created_at",
        "message_id", "message_pair_id",
    }
    # 已消费记录绑定本轮 user 消息 id 与配对 id
    assert uploads[0]["message_id"] == 101
    assert uploads[0]["message_pair_id"] == "pair-1"
    # 未消费记录两者为 null
    assert uploads[1]["message_id"] is None
    assert uploads[1]["message_pair_id"] is None
    assert uploads[1]["name"] == "语音.m4a"


@pytest.mark.asyncio
async def test_get_session_detail_messages_and_files_new_fields():
    """messages 透出 message_pair_id/citations，files 透出 message_id/message_pair_id。"""
    dao = _make_dao()
    svc = SessionService(dao, upload_file_dao=_make_upload_dao())

    detail = await svc.get_session_detail("s1", "u1")

    dumped = detail.model_dump()
    msgs = dumped["messages"]
    assert msgs[0]["message_pair_id"] == "pair-1"
    assert msgs[0]["citations"] == []
    assert msgs[1]["message_pair_id"] == "pair-1"
    assert msgs[1]["citations"] == [{"title": "差旅办法", "url": "http://x"}]
    files = dumped["files"]
    assert files[0]["message_id"] == 102
    assert files[0]["message_pair_id"] == "pair-1"


@pytest.mark.asyncio
async def test_get_session_detail_upload_files_empty_when_dao_missing():
    """upload_file_dao 未注入时 upload_files 为 []（且不调用任何上传 DAO）。"""
    dao = _make_dao()
    svc = SessionService(dao, upload_file_dao=None)

    detail = await svc.get_session_detail("s1", "u1")

    assert detail.upload_files == []
    assert isinstance(detail.model_dump()["upload_files"], list)


@pytest.mark.asyncio
async def test_get_session_detail_upload_files_degrades_on_error():
    """list_files_by_session 抛异常时降级为 []，详情接口不抛出。"""
    dao = _make_dao()
    upload_dao = _make_upload_dao(exc=RuntimeError("db down"))
    svc = SessionService(dao, upload_file_dao=upload_dao)

    detail = await svc.get_session_detail("s1", "u1")

    assert detail.upload_files == []
    # 其余字段照常加载
    assert len(detail.messages) == 2
    assert len(detail.files) == 1


@pytest.mark.asyncio
async def test_get_session_detail_upload_files_empty_rows():
    """会话无上传文件：upload_files 为空列表。"""
    dao = _make_dao()
    upload_dao = _make_upload_dao(rows=[])
    svc = SessionService(dao, upload_file_dao=upload_dao)

    detail = await svc.get_session_detail("s1", "u1")

    assert detail.upload_files == []
