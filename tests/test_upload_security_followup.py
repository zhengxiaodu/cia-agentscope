import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.services.session_service import SessionService
from app.routes import upload, files, message_share
from app.services import file_parse_service as fps
from app.models.session import SessionDetailResponse


class ReservationDAO:
    def __init__(self):
        self.rows = {}

    async def reserve_session(self, sid, uid):
        return self.rows.setdefault(sid, {'user_id': uid, 'deleted_at': None})

    async def get_session_meta(self, sid):
        return self.rows.get(sid)

    async def get_session_owner_status(self, sid):
        return self.rows.get(sid)


@pytest.mark.asyncio
async def test_first_upload_reserves_owner_before_first_chat():
    dao = ReservationDAO()
    service = SessionService(dao)
    sid = await service.get_or_create_session(None, 'A')
    assert dao.rows[sid]['user_id'] == 'A'
    with pytest.raises(HTTPException) as error:
        await service.get_or_create_session(sid, 'B')
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_upload_capacity_rejects_before_read_and_releases_on_failure(monkeypatch):
    admission = fps.UploadAdmission(1)
    monkeypatch.setattr(fps, '_UPLOAD_ADMISSION', admission)
    lease = admission.acquire()
    file = SimpleNamespace(size=5, content_type='text/plain', filename='a.txt',
                           read=AsyncMock(return_value=b'hello'))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_service=SessionService(ReservationDAO()))))
    with pytest.raises(HTTPException) as error:
        await upload.upload_file(file, None, {'user_id': 'A'}, req)
    assert error.value.status_code == 429
    file.read.assert_not_awaited()
    lease.release()
    file.read.side_effect = OSError('read failed')
    with pytest.raises(OSError):
        await upload.upload_file(file, None, {'user_id': 'A'}, req)
    assert admission.active == 0


@pytest.mark.asyncio
async def test_cancel_before_parser_starts_releases_admission(monkeypatch):
    admission = fps.UploadAdmission(1)
    monkeypatch.setattr(fps, '_UPLOAD_ADMISSION', admission)
    dao = SimpleNamespace(insert=AsyncMock(return_value=7))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(upload_file_dao=dao)))
    before = set(asyncio.all_tasks())
    await fps.start_background_parse(req, 's1', 'A', 'a.txt', 'text/plain', b'hello')
    tasks = asyncio.all_tasks() - before
    assert len(tasks) == 1
    assert admission.active == 1
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert admission.active == 0


def share_client(tmp_path, monkeypatch):
    monkeypatch.setattr(files, 'SESSION_FILES_PERSIST_DIR', str(tmp_path))
    (tmp_path / 's1').mkdir()
    (tmp_path / 's1' / 'a.txt').write_bytes(b'shared')
    (tmp_path / 's1' / 'private.txt').write_bytes(b'private')
    detail = SessionDetailResponse(session_id='s1', created_at='', updated_at='',
        messages=[{'role': 'assistant', 'content': 'answer', 'timestamp': '', 'message_pair_id': 'p1'}],
        files=[{'name': n, 'path': n, 'url': '/persist-files/s1/' + n,
                'size': 6, 'media_type': 'text/plain', 'message_pair_id': p}
               for n,p in [('a.txt','p1'),('private.txt','p2')]])
    app = FastAPI()
    app.include_router(message_share.router)
    app.include_router(files.router)
    app.state.message_share_dao = SimpleNamespace(get_share=AsyncMock(return_value={
        'session_id': 's1', 'user_id': 'A', 'message_pair_ids': ['p1'], 'title': 'share'}))
    app.state.session_service = SimpleNamespace(get_session_detail=AsyncMock(return_value=detail),
        session_files_accessible=AsyncMock(return_value=True))
    return TestClient(app), app, detail


def test_share_url_downloads_only_selected_files(tmp_path, monkeypatch):
    client, app, detail = share_client(tmp_path, monkeypatch)
    data = client.get('/message_share/token').json()['data']
    url = data['files'][0]['url']
    assert url.startswith('/message_share/token/files/')
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == b'shared'
    assert response.headers['content-disposition'].startswith('attachment;')
    assert client.get('/message_share/token/files/private.txt').status_code == 404
    detail.messages.clear()
    assert client.get(url).status_code == 404
    app.state.message_share_dao.get_share.return_value = None
    assert client.get(url).status_code == 404


def test_deleted_session_invalidates_share_download(tmp_path, monkeypatch):
    client, app, _ = share_client(tmp_path, monkeypatch)
    app.state.session_service.get_session_detail.return_value = None
    assert client.get('/message_share/token/files/a.txt').status_code == 404
