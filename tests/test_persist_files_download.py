"""持久化文件下载接口 GET /persist-files/{session_id}/{path} 的路由测试。

无现成 routes 层测试基建（test_upload_query.py 自建最小 app），沿用其做法：
最小 FastAPI app 仅挂被测路由，override current_user 依赖，monkeypatch
app.routes.files.SESSION_FILES_PERSIST_DIR 为 tmp_path，app.state 挂 mock
session_service（属主校验默认放行）。
覆盖：200 下载（含中文文件名 RFC 5987 编码与子目录路径）、404、403 穿越
（path 含 .. / session_id 为 ..）、403 非属主/已删除/会话不存在、
503 未配置、mode=inline 文本预览。
"""
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.files as files_route
from app.dependencies import current_user


class _StubSessionService:
    """属主校验桩：accessible 集合外的 session 一律 403。"""

    def __init__(self, accessible=None, users=("u1",)):
        self.accessible = {"s1", "s2"} if accessible is None else set(accessible)
        self.users = set(users)
        self.calls = []

    async def session_files_accessible(self, session_id, user_id):
        self.calls.append((session_id, user_id))
        return session_id in self.accessible and user_id in self.users


def _make_app(session_service=None) -> FastAPI:
    app = FastAPI()
    app.include_router(files_route.router)
    app.dependency_overrides[current_user] = lambda: {"user_id": "u1"}
    app.state.session_service = session_service or _StubSessionService()
    return app


@pytest.fixture
def client():
    return TestClient(_make_app())


# ---------------------------------------------------------------------------
# 200：下载成功
# ---------------------------------------------------------------------------

def test_download_200_chinese_filename(client, tmp_path, monkeypatch):
    """中文文件名下载成功：Content-Disposition 含 filename*=UTF-8'' 百分号编码。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    content = "报告内容".encode("utf-8")
    target = tmp_path / "s1" / "报告.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)

    resp = client.get("/persist-files/s1/报告.md")

    assert resp.status_code == 200
    assert resp.content == content
    assert resp.headers["content-type"].startswith("text/markdown")
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;")
    # RFC 5987：filename*=UTF-8''<percent-encoded 原文件名>
    assert f"filename*=UTF-8''{quote('报告.md')}" in cd


def test_download_200_keeps_subdirectory(client, tmp_path, monkeypatch):
    """子目录路径正常下载（不误伤合法相对路径）。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    content = b"\x00\x01docx"
    target = tmp_path / "s1" / "report" / "b.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)

    resp = client.get("/persist-files/s1/report/b.docx")

    assert resp.status_code == 200
    assert resp.content == content


# ---------------------------------------------------------------------------
# 404：文件不存在
# ---------------------------------------------------------------------------

def test_download_404_when_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    (tmp_path / "s1").mkdir(parents=True)

    resp = client.get("/persist-files/s1/missing.txt")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 403：路径穿越
# ---------------------------------------------------------------------------

def test_download_403_when_path_contains_dotdot(client, tmp_path, monkeypatch):
    """path 含 ..（%2e%2e 编码以避免 httpx 归一化删除点段）→ 403。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    # 预置穿越目标文件，证明 403 先于任何文件读取
    (tmp_path / "evil.txt").write_bytes(b"secret")

    resp = client.get("/persist-files/s1/%2e%2e/evil.txt")

    assert resp.status_code == 403


def test_download_403_when_session_id_is_dotdot(client, tmp_path, monkeypatch):
    """session_id 为 .. → 403。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))

    resp = client.get("/persist-files/%2e%2e/evil.txt")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 503：持久化目录未配置
# ---------------------------------------------------------------------------

def test_download_503_when_persist_dir_not_configured(client, tmp_path, monkeypatch):
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", "")
    target = tmp_path / "s1" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    resp = client.get("/persist-files/s1/a.txt")

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# mode=inline：文本文件内联预览
# ---------------------------------------------------------------------------

def test_download_inline_text_with_charset(client, tmp_path, monkeypatch):
    """mode=inline：文本文件返回 inline，Content-Type 追加 charset=utf-8。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    target = tmp_path / "s1" / "note.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes("文本内容".encode("utf-8"))

    resp = client.get("/persist-files/s1/note.txt", params={"mode": "inline"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/plain; charset=utf-8"
    assert resp.headers["content-disposition"] == "inline"
    assert resp.content == "文本内容".encode("utf-8")


def test_download_default_mode_is_attachment(client, tmp_path, monkeypatch):
    """默认 mode=download：附件下载头。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    target = tmp_path / "s1" / "note.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"plain")

    resp = client.get("/persist-files/s1/note.txt")

    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment;")


# ---------------------------------------------------------------------------
# 403：属主校验（P1-1）
# ---------------------------------------------------------------------------

def test_download_403_when_not_session_owner(tmp_path, monkeypatch):
    """登录用户访问他人会话文件 → 403（校验先于文件存在性判断）。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    stub = _StubSessionService(accessible=set())  # 任何会话都判非属主
    client = TestClient(_make_app(stub))
    # 预置他人文件，证明 403 先于任何文件读取
    target = tmp_path / "s1" / "secret.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"secret")

    resp = client.get("/persist-files/s1/secret.txt")

    assert resp.status_code == 403
    assert stub.calls == [("s1", "u1")]


def test_download_403_when_session_service_missing(tmp_path, monkeypatch):
    """app.state 未挂 session_service（配置异常）→ 403 拒绝而非放行。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    client = TestClient(_make_app(session_service=None))
    app = client.app
    if hasattr(app.state, "session_service"):
        del app.state.session_service  # 模拟未初始化

    resp = client.get("/persist-files/s1/a.txt")

    assert resp.status_code == 403


def test_download_403_takes_precedence_over_404(tmp_path, monkeypatch):
    """属主不匹配时即使文件存在也 403，且不泄露会话是否存在。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    stub = _StubSessionService(accessible={"s2"})  # s1 不属于 u1
    client = TestClient(_make_app(stub))
    target = tmp_path / "s1" / "exists.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    resp = client.get("/persist-files/s1/exists.txt")

    assert resp.status_code == 403


def test_download_200_after_ownership_check(tmp_path, monkeypatch):
    """属主校验通过后正常下载（校验参数含当前用户）。"""
    monkeypatch.setattr(files_route, "SESSION_FILES_PERSIST_DIR", str(tmp_path))
    stub = _StubSessionService(accessible={"s1"})
    client = TestClient(_make_app(stub))
    target = tmp_path / "s1" / "mine.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"ok")

    resp = client.get("/persist-files/s1/mine.txt")

    assert resp.status_code == 200
    assert resp.content == b"ok"
    assert stub.calls == [("s1", "u1")]
