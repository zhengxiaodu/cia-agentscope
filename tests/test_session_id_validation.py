"""session_id 安全入口校验与归档路径防御的单元测试。

覆盖安全修复：
- P0-2：get_or_create_session 入口格式校验（防路径穿越/shell 注入）、
  _persist_file_bytes 归档路径 realpath 越界拒绝
- P1-2：上传接口携带 session_id 时必须经 get_or_create_session（属主 403 透传）
- P1-3：已删除会话拒绝复用（403 区分『已删除』与『不存在』）
- P1-1 配套：session_files_accessible 属主/软删校验
"""
import os
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.dependencies import current_user
from app.routes.upload import router as upload_router
from app.services.chat_service import _persist_file_bytes
from app.services.session_service import SessionService


# ---------------------------------------------------------------------------
# 测试桩
# ---------------------------------------------------------------------------

class _StubDao:
    def __init__(self, meta=None, owner_status=None, reserved=None):
        self.meta = meta
        self.owner_status = owner_status
        # reserve_session 统一登记契约：默认首次认领成功（属主=本人、未删）
        self.reserved = reserved if reserved is not None else {
            "user_id": "u1", "deleted_at": None,
        }

    async def get_session_meta(self, session_id):
        return self.meta

    async def get_session_owner_status(self, session_id):
        return self.owner_status

    async def reserve_session(self, session_id, user_id):
        return self.reserved


class _RecordingSessionService:
    """记录调用并按预设抛错的会话服务桩（模拟属主校验拒绝）。"""

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    async def get_or_create_session(self, session_id, user_id):
        self.calls.append((session_id, user_id))
        if self.exc is not None:
            raise self.exc
        return session_id or uuid.uuid4().hex


# ---------------------------------------------------------------------------
# P0-2：入口格式校验
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [
    "../outside",          # 路径穿越
    "..",                 
    "a/b",                 # 斜杠
    "a\\b",                # 反斜杠
    "a b",                 # 空格
    "a;rm",                # shell 元字符
    "$(id)",               # 命令替换
    "a'b",                 # 引号
    "a" * 65,              # 超长
])
async def test_get_or_create_rejects_bad_format(bad_id):
    svc = SessionService(_StubDao())
    with pytest.raises(HTTPException) as exc_info:
        await svc.get_or_create_session(bad_id, "u1")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("good_id", [
    uuid.uuid4().hex,      # 常规会话 ID
    "session-1",           # 历史兼容：连字符
    "S_2024",              # 历史兼容：下划线/大写
])
async def test_get_or_create_accepts_valid_format(good_id):
    svc = SessionService(_StubDao())  # 不存在 → 惰性放行
    assert await svc.get_or_create_session(good_id, "u1") == good_id


@pytest.mark.asyncio
async def test_get_or_create_new_session_is_uuid_hex():
    svc = SessionService(_StubDao())
    new_id = await svc.get_or_create_session(None, "u1")
    assert len(new_id) == 32
    assert all(c in "0123456789abcdef" for c in new_id)


# ---------------------------------------------------------------------------
# P1-3：已删除会话拒绝复用
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_or_create_rejects_deleted_session():
    svc = SessionService(_StubDao(reserved={"user_id": "u1", "deleted_at": "2026-01-01"}))
    with pytest.raises(HTTPException) as exc_info:
        await svc.get_or_create_session("s1", "u1")
    assert exc_info.value.status_code == 403
    assert "已删除" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_or_create_allows_nonexistent_session():
    """不存在的会话 ID：首次使用原子登记属主（=本人）后放行。"""
    svc = SessionService(_StubDao())
    assert await svc.get_or_create_session("s1", "u1") == "s1"


@pytest.mark.asyncio
async def test_get_or_create_rejects_foreign_session():
    svc = SessionService(_StubDao(reserved={"user_id": "u2", "deleted_at": None}))
    with pytest.raises(HTTPException) as exc_info:
        await svc.get_or_create_session("s1", "u1")
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# P1-1 配套：session_files_accessible
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("owner_status,expected", [
    ({"user_id": "u1", "deleted_at": None}, True),          # 属主 + 未删
    ({"user_id": "u1", "deleted_at": "2026-01-01"}, False), # 属主但已软删
    ({"user_id": "u2", "deleted_at": None}, False),         # 他人会话
    (None, False),                                          # 会话不存在
])
async def test_session_files_accessible(owner_status, expected):
    svc = SessionService(_StubDao(owner_status=owner_status))
    assert await svc.session_files_accessible("s1", "u1") is expected


# ---------------------------------------------------------------------------
# P0-2：_persist_file_bytes 归档路径防御
# ---------------------------------------------------------------------------

def test_persist_file_bytes_rejects_traversal_session_id(tmp_path):
    """session_id 含 ../ 时拒绝写入，归档根外不产生任何文件。"""
    root = tmp_path / "persist"
    root.mkdir()
    ok = _persist_file_bytes(str(root), "../outside", "evil.txt", b"x")
    assert ok is False
    assert not (tmp_path / "outside").exists()
    assert list(root.iterdir()) == []


def test_persist_file_bytes_rejects_traversal_rel_path(tmp_path):
    """rel_path 含 ../ 时同样拒绝（防御纵深，不依赖入口校验）。"""
    root = tmp_path / "persist"
    root.mkdir()
    (tmp_path / "evil.txt").write_bytes(b"pre")  # 预置穿越目标，证明未被覆盖
    ok = _persist_file_bytes(str(root), "s1", "../evil.txt", b"x")
    assert ok is False
    assert (tmp_path / "evil.txt").read_bytes() == b"pre"


def test_persist_file_bytes_normal_write(tmp_path):
    """合法路径正常写入且内容一致。"""
    root = tmp_path / "persist"
    root.mkdir()
    ok = _persist_file_bytes(str(root), "s1", "sub/a.txt", b"hello")
    assert ok is True
    assert (root / "s1" / "sub" / "a.txt").read_bytes() == b"hello"


# ---------------------------------------------------------------------------
# P1-2：上传接口属主校验透传
# ---------------------------------------------------------------------------

def _upload_app(session_service, monkeypatch=None):
    app = FastAPI()
    app.include_router(upload_router)
    app.dependency_overrides[current_user] = lambda: {"user_id": "u1"}
    app.state.session_service = session_service
    if monkeypatch is not None:
        # 打桩后台解析：测试只关注属主校验链路，不启动真实解析任务
        monkeypatch.setattr(
            "app.routes.upload.start_background_parse",
            _fake_start_background_parse,
        )
    return app


async def _fake_start_background_parse(request, session_id, user_id, filename, media_type, content, admission=None):
    return 42


def test_upload_passes_session_id_through_ownership_check(monkeypatch):
    """携带 session_id 的上传必须经 get_or_create_session（参数正确透传）。"""
    stub = _RecordingSessionService()
    client = TestClient(_upload_app(stub, monkeypatch))

    resp = client.post(
        "/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"session_id": "s1"},
    )

    assert resp.status_code == 200
    assert stub.calls == [("s1", "u1")]


def test_upload_foreign_session_403_passthrough():
    """他人会话的上传 → 403 透传，不落库不解析。"""
    stub = _RecordingSessionService(exc=HTTPException(status_code=403, detail="无权访问该会话"))
    client = TestClient(_upload_app(stub))

    resp = client.post(
        "/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"session_id": "s1"},
    )

    assert resp.status_code == 403


def test_upload_without_session_id_creates_session(monkeypatch):
    """未传 session_id 时由服务端生成（记录调用参数为 None）。"""
    stub = _RecordingSessionService()
    client = TestClient(_upload_app(stub, monkeypatch))

    resp = client.post(
        "/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )

    assert resp.status_code == 200
    assert stub.calls == [(None, "u1")]
    data = resp.json()["data"]["session_id"]
    assert len(data) == 32
