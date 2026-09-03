"""file_parse_service 单元测试：分类、MinerU/ASR 解析（mock httpx）、后台解析入库流转。"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services import file_parse_service as fps


# ---------------------------------------------------------------------------
# classify_parse_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("media_type,filename,expected", [
    ("application/pdf", "报告.pdf", "mineru"),
    ("", "报告.docx", "mineru"),
    ("", "表格.xlsx", "mineru"),
    ("", "图片.png", "mineru"),
    ("image/jpeg", "照片.jpg", "mineru"),
    ("audio/mp4", "录音.m4a", "asr"),
    ("audio/mpeg", "歌曲.mp3", "asr"),
    ("", "语音.wav", "asr"),
    ("", "voice.webm", "asr"),
    ("text/plain", "笔记.txt", "plain_text"),
    ("", "readme.md", "plain_text"),
    ("application/vnd.ms-powerpoint", "幻灯.ppt", "none"),
    ("application/zip", "压缩.zip", "none"),
])
def test_classify_parse_type(media_type, filename, expected):
    assert fps.classify_parse_type(media_type, filename) == expected


# ---------------------------------------------------------------------------
# parse_document_with_mineru（mock httpx.AsyncClient）
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """按调用序列返回预置响应的 httpx.AsyncClient 替身。"""

    def __init__(self, post_responses, get_responses):
        self._posts = list(post_responses)
        self._gets = list(get_responses)
        self.post_calls = []
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._posts.pop(0)

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._gets.pop(0)


@pytest.fixture(autouse=True)
def _mineru_configured(monkeypatch):
    """测试环境默认无 MinerU 配置，统一注入假配置。"""
    monkeypatch.setattr(fps, "MINERU_API_KEY", "test-key")
    monkeypatch.setattr(fps, "MINERU_BASE_URL", "http://mineru.example")


@pytest.mark.asyncio
async def test_mineru_success():
    post = _FakeResponse(201, {"id": "task-1"})
    get = _FakeResponse(200, {
        "status": "completed",
        "result": {"md_content": "# 解析结果"},
    })
    client = _FakeClient([post], [get])
    with patch.object(fps.httpx, "AsyncClient", return_value=client):
        result = await fps.parse_document_with_mineru(b"fake-pdf", "a.pdf")
    assert result == "# 解析结果"
    # 提交参数：multipart 文件名 + output_formats + x-api-key 鉴权
    url, kwargs = client.post_calls[0]
    assert url.endswith("/tasks")
    assert kwargs["headers"]["x-api-key"] == "test-key"
    assert kwargs["files"]["file"][0] == "a.pdf"
    assert kwargs["files"]["file"][1] == b"fake-pdf"
    assert kwargs["data"]["output_formats"] == "md"


@pytest.mark.asyncio
async def test_mineru_submit_failure():
    post = _FakeResponse(500, text="server error")
    client = _FakeClient([post], [])
    with patch.object(fps.httpx, "AsyncClient", return_value=client):
        result = await fps.parse_document_with_mineru(b"x", "a.pdf")
    assert result.startswith("解析失败")
    assert "500" in result


@pytest.mark.asyncio
async def test_mineru_timeout_returns_honest_message(monkeypatch):
    """轮询始终 pending → 60s 超时后返回统一超时文案。"""
    post = _FakeResponse(201, {"id": "task-1"})
    pending = _FakeResponse(200, {"status": "pending"})
    # 轮询间隔放大到 20s：3 次 GET 后 elapsed=60 触发超时；sleep mock 掉加速测试
    client = _FakeClient([post], [pending] * 5)
    monkeypatch.setattr(fps, "_MINERU_POLL_INTERVAL", 20)
    monkeypatch.setattr(fps.asyncio, "sleep", AsyncMock(return_value=None))
    with patch.object(fps.httpx, "AsyncClient", return_value=client):
        result = await fps.parse_document_with_mineru(b"x", "a.pdf")
    assert result == fps._MINERU_TIMEOUT_MSG
    assert result == "解析超时，MinerU服务暂时无法解析该文件"


@pytest.mark.asyncio
async def test_mineru_task_failed():
    post = _FakeResponse(201, {"id": "task-1"})
    failed = _FakeResponse(200, {"status": "failed", "error_message": "bad file"})
    client = _FakeClient([post], [failed])
    with patch.object(fps.httpx, "AsyncClient", return_value=client):
        result = await fps.parse_document_with_mineru(b"x", "a.pdf")
    assert result.startswith("解析失败")
    assert "bad file" in result


# ---------------------------------------------------------------------------
# transcribe_audio（mock httpx + 模型配置）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transcribe_audio_success(monkeypatch):
    cfg = {
        "models": {
            "audio_transcriber": {
                "provider": "openai",
                "model_name": "Qwen3-ASR-1.7B",
                "base_url": "http://asr.example",
                "api_key": "sk-test",
                "parameters": {"prompt": "解析音频", "language": "zh"},
            }
        }
    }
    monkeypatch.setattr(
        "app.services.chat_service.load_model_config", lambda path=None: cfg
    )
    resp = _FakeResponse(200, {"text": "测试语音"})
    client = _FakeClient([resp], [])
    with patch.object(fps.httpx, "AsyncClient", return_value=client):
        result = await fps.transcribe_audio(b"fake-audio", "t.m4a")
    assert result == "测试语音"
    url, kwargs = client.post_calls[0]
    assert url == "http://asr.example/v1/audio/transcriptions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["data"]["model"] == "Qwen3-ASR-1.7B"
    assert kwargs["data"]["prompt"] == "解析音频"
    assert kwargs["data"]["language"] == "zh"
    assert kwargs["files"]["file"][0] == "t.m4a"


@pytest.mark.asyncio
async def test_transcribe_audio_timeout(monkeypatch):
    cfg = {"models": {"audio_transcriber": {
        "base_url": "http://asr.example", "api_key": "k",
        "model_name": "Qwen3-ASR-1.7B", "parameters": {},
    }}}
    monkeypatch.setattr(
        "app.services.chat_service.load_model_config", lambda path=None: cfg
    )
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    with patch.object(fps.httpx, "AsyncClient", return_value=ctx):
        result = await fps.transcribe_audio(b"x", "t.m4a")
    assert result == "解析超时，音频解析服务暂时无法解析该文件"


@pytest.mark.asyncio
async def test_transcribe_audio_missing_config(monkeypatch):
    monkeypatch.setattr(
        "app.services.chat_service.load_model_config", lambda path=None: {"models": {}}
    )
    result = await fps.transcribe_audio(b"x", "t.m4a")
    assert result.startswith("解析失败")
    assert "audio_transcriber" in result


# ---------------------------------------------------------------------------
# _parse_and_store / start_background_parse（mock dao）
# ---------------------------------------------------------------------------

class _FakeDao:
    def __init__(self, file_id=1):
        self.file_id = file_id
        self.calls = []

    async def insert(self, session_id, filename, media_type, parse_type):
        self.calls.append(("insert", session_id, filename, media_type, parse_type))
        return self.file_id

    async def mark_parsing(self, file_id):
        self.calls.append(("mark_parsing", file_id))

    async def update_parse_result(self, file_id, status, parsed_content, error_message=None):
        self.calls.append(("update", file_id, status, parsed_content, error_message))


@pytest.mark.asyncio
async def test_parse_and_store_plain_text():
    dao = _FakeDao()
    await fps._parse_and_store(dao, 7, "你好世界".encode("utf-8"), "note.txt", "plain_text")
    assert ("mark_parsing", 7) in dao.calls
    update = [c for c in dao.calls if c[0] == "update"][0]
    assert update[2] == "completed"
    assert update[3] == "你好世界"


@pytest.mark.asyncio
async def test_parse_and_store_mineru_failure_stores_honest_message():
    dao = _FakeDao()
    with patch.object(
        fps, "parse_document_with_mineru",
        AsyncMock(return_value=fps._MINERU_TIMEOUT_MSG),
    ):
        await fps._parse_and_store(dao, 3, b"x", "a.pdf", "mineru")
    update = [c for c in dao.calls if c[0] == "update"][0]
    assert update[2] == "failed"
    assert update[3] == fps._MINERU_TIMEOUT_MSG


@pytest.mark.asyncio
async def test_parse_and_store_none_type_no_content():
    dao = _FakeDao()
    await fps._parse_and_store(dao, 5, b"x", "a.ppt", "none")
    update = [c for c in dao.calls if c[0] == "update"][0]
    assert update[2] == "completed"
    assert update[3] is None


@pytest.mark.asyncio
async def test_start_background_parse_inserts_and_schedules_task():
    dao = _FakeDao(file_id=42)
    request = MagicMock()
    request.app.state.upload_file_dao = dao
    with patch.object(fps, "_parse_and_store", AsyncMock()) as mock_parse:
        file_id = await fps.start_background_parse(
            request, "s1", "a.pdf", "application/pdf", b"content"
        )
        assert file_id == 42
        # insert 参数正确
        assert dao.calls[0] == ("insert", "s1", "a.pdf", "application/pdf", "mineru")
        # 等后台任务被调度执行
        await asyncio.sleep(0.01)
        mock_parse.assert_awaited_once_with(dao, 42, b"content", "a.pdf", "mineru")


@pytest.mark.asyncio
async def test_start_background_parse_requires_dao():
    request = MagicMock()
    request.app.state.upload_file_dao = None
    with pytest.raises(RuntimeError):
        await fps.start_background_parse(request, "s1", "a.pdf", "application/pdf", b"x")
