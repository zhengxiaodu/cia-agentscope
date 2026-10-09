"""持久化文件下载接口 GET /persist-files/{session_id}/{path} 的路由测试。

无现成 routes 层测试基建（test_upload_query.py 自建最小 app），沿用其做法：
最小 FastAPI app 仅挂被测路由，override current_user 依赖，monkeypatch
app.routes.files.SESSION_FILES_PERSIST_DIR 为 tmp_path。
覆盖：200 下载（含中文文件名 RFC 5987 编码与子目录路径）、404、403 穿越
（path 含 .. / session_id 为 ..）、503 未配置、mode=inline 文本预览。
"""
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.files as files_route
from app.dependencies import current_user


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(files_route.router)
    app.dependency_overrides[current_user] = lambda: {"user_id": "u1"}
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
