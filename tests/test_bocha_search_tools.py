"""tools/bocha_search_tools.py 单测。

覆盖：
- 工厂：命名、描述非空、可直接 await 调用
- 入参校验：空 query / 空白 query 不发起 HTTP 请求
- 配置校验：BOCHA_API_KEY 为空时返回未配置提示，不外呼
- 成功路径：请求 url/headers/payload 正确；返回文本含序号/标题/链接；
  bocha_sum 字段子集写入 ToolChunk.metadata
- 失败路径：超时 / 网络异常 / HTTP 非 200 / 业务错误码 / 响应解析失败 /
  无搜索结果，均返回友好文本且 metadata 为空
- 辅助函数：_extract_bocha_sum 只保留子集字段、缺失字段补空串；
  _build_result 无 bocha_sum 时 metadata 为空 dict
"""
import httpx
import pytest

import tools.bocha_search_tools as bocha_module
from tools.bocha_search_tools import (
    _BOCHA_SUM_FIELDS,
    _build_result,
    _extract_bocha_sum,
    _format_search_results,
    create_bocha_search_tool,
)

# ---------------------------------------------------------------------------
# 假 httpx 客户端（记录请求，可注入响应或异常）
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


class _FakeAsyncClient:
    """替代 httpx.AsyncClient：记录 post 调用，返回预设响应或抛异常。"""

    last_instance = None

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.post_calls = []
        _FakeAsyncClient.last_instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        if self._exc is not None:
            raise self._exc
        return self._resp


def _patch_httpx(monkeypatch, resp=None, exc=None):
    fake = _FakeAsyncClient(resp=resp, exc=exc)
    monkeypatch.setattr(bocha_module.httpx, "AsyncClient", lambda *a, **kw: fake)
    return fake


def _patch_config(monkeypatch, api_key="test-key", api_base="https://api.bocha.cn"):
    monkeypatch.setattr(bocha_module, "BOCHA_API_KEY", api_key)
    monkeypatch.setattr(bocha_module, "BOCHA_API_BASE", api_base)


def _make_success_payload(pages):
    """构造博查 web-search 成功响应体。"""
    return {
        "code": 200,
        "log_id": "abc",
        "msg": None,
        "data": {"webPages": {"value": pages}},
    }


_PAGES = [
    {
        "name": "标题一",
        "url": "https://example.com/1",
        "snippet": "片段一",
        "summary": "摘要一",
        "siteName": "站点一",
        "dateLastCrawled": "2026-01-01",
        "extraField": "不应进入 bocha_sum",
    },
    {
        "name": "标题二",
        "url": "https://example.com/2",
        "snippet": "片段二",
        # summary/siteName/dateLastCrawled 缺失 → 补空串
    },
]


def _chunk_text(chunk) -> str:
    return chunk.content[0].text


# ---------------------------------------------------------------------------
# 工厂与入参/配置校验
# ---------------------------------------------------------------------------


def test_tool_factory_returns_named_tool():
    tool = create_bocha_search_tool()
    assert tool.name == "bocha_web_search"
    assert tool.description


@pytest.mark.asyncio
async def test_empty_query_rejected_without_http_call(monkeypatch):
    """空/空白 query：直接返回错误文本，不发起 HTTP 请求。"""
    _patch_config(monkeypatch)
    fake = _patch_httpx(monkeypatch)
    tool = create_bocha_search_tool()

    for q in ("", "   "):
        chunk = await tool(query=q)
        assert _chunk_text(chunk) == "错误：搜索关键词不能为空。"
        assert chunk.metadata == {}
    assert fake.post_calls == []


@pytest.mark.asyncio
async def test_missing_api_key_returns_unconfigured(monkeypatch):
    """API key 未配置：返回未配置提示，不外呼。"""
    _patch_config(monkeypatch, api_key="")
    fake = _patch_httpx(monkeypatch)
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索服务未配置，请联系管理员。"
    assert chunk.metadata == {}
    assert fake.post_calls == []


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_returns_text_and_bocha_sum_metadata(monkeypatch):
    """成功：请求 url/headers/payload 正确；文本含序号标题链接；
    bocha_sum 字段子集写入 metadata。"""
    _patch_config(monkeypatch, api_key="sk-test")
    fake = _patch_httpx(
        monkeypatch, resp=_FakeResponse(json_data=_make_success_payload(_PAGES)),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="今日科技新闻")

    # 请求形态：{base}/v1/web-search、Bearer 鉴权、只传 query
    assert len(fake.post_calls) == 1
    call = fake.post_calls[0]
    assert call["url"] == "https://api.bocha.cn/v1/web-search"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["json"] == {"query": "今日科技新闻"}

    # 返回文本：序号 + 标题 + 摘要/片段 + 链接
    text = _chunk_text(chunk)
    assert "[1] 标题一" in text
    assert "摘要一" in text
    assert "链接: https://example.com/1" in text
    assert "[2] 标题二" in text

    # metadata.bocha_sum：只保留子集字段，缺失补空串
    bocha_sum = chunk.metadata["bocha_sum"]
    assert len(bocha_sum) == 2
    assert bocha_sum[0] == {
        "name": "标题一", "url": "https://example.com/1", "snippet": "片段一",
        "summary": "摘要一", "siteName": "站点一", "dateLastCrawled": "2026-01-01",
    }
    assert bocha_sum[1]["summary"] == ""
    assert bocha_sum[1]["siteName"] == ""
    assert all("extraField" not in s for s in bocha_sum)


@pytest.mark.asyncio
async def test_api_base_trailing_slash_normalized(monkeypatch):
    """API base 带尾部斜杠时拼接仍正确（rstrip 处理）。"""
    _patch_config(monkeypatch, api_base="https://api.bocha.cn/")
    fake = _patch_httpx(
        monkeypatch, resp=_FakeResponse(json_data=_make_success_payload(_PAGES)),
    )
    tool = create_bocha_search_tool()

    await tool(query="q")
    assert fake.post_calls[0]["url"] == "https://api.bocha.cn/v1/web-search"


# ---------------------------------------------------------------------------
# 失败路径（均不写 metadata）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_friendly_message(monkeypatch):
    _patch_config(monkeypatch)
    _patch_httpx(monkeypatch, exc=httpx.TimeoutException("timed out"))
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索请求超时，请稍后重试。"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_network_error_returns_friendly_message(monkeypatch):
    _patch_config(monkeypatch)
    _patch_httpx(monkeypatch, exc=ConnectionError("dns fail"))
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索网络异常: dns fail"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_http_error_status_returns_friendly_message(monkeypatch):
    _patch_config(monkeypatch)
    _patch_httpx(
        monkeypatch, resp=_FakeResponse(status_code=403, text="forbidden"),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索接口返回错误: HTTP 403"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_invalid_json_returns_friendly_message(monkeypatch):
    _patch_config(monkeypatch)
    _patch_httpx(monkeypatch, resp=_FakeResponse(status_code=200, text="not json"))
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索响应解析失败，请稍后重试。"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_business_error_code_returns_friendly_message(monkeypatch):
    """code != 200 且非 None：业务错误。"""
    _patch_config(monkeypatch)
    _patch_httpx(
        monkeypatch, resp=_FakeResponse(json_data={"code": 40001, "msg": "bad"}),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索服务返回错误，请稍后重试。"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_no_web_pages_returns_no_result(monkeypatch):
    """webPages.value 为空列表或缺失：未搜索到相关结果。"""
    _patch_config(monkeypatch)
    _patch_httpx(
        monkeypatch,
        resp=_FakeResponse(json_data={"code": 200, "data": {"webPages": {"value": []}}}),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="不存在的关键词xyz")
    assert _chunk_text(chunk) == "未搜索到相关结果。"
    assert chunk.metadata == {}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def test_extract_bocha_sum_field_subset():
    """只保留 _BOCHA_SUM_FIELDS 子集，缺失字段补空串，非 dict 项跳过。"""
    pages = [
        {"name": "n", "url": "u", "unexpected": "x"},
        "not-a-dict",
        None,
    ]
    result = _extract_bocha_sum(pages)
    assert len(result) == 1
    assert set(result[0].keys()) == set(_BOCHA_SUM_FIELDS)
    assert result[0]["name"] == "n"
    assert result[0]["snippet"] == ""


def test_format_search_results_prefers_summary_over_snippet():
    """格式化文本 summary 优先，缺失时回退 snippet。"""
    bocha_sum = [
        {"name": "A", "url": "http://a", "summary": "摘要A", "snippet": "片段A"},
        {"name": "B", "url": "http://b", "summary": "", "snippet": "片段B"},
    ]
    text = _format_search_results(bocha_sum)
    assert "[1] A" in text and "摘要A" in text
    assert "[2] B" in text and "片段B" in text
    assert "链接: http://a" in text and "链接: http://b" in text


def test_build_result_with_bocha_sum():
    chunk = _build_result("text", bocha_sum=[{"name": "x"}])
    assert chunk.is_last is True
    assert chunk.metadata == {"bocha_sum": [{"name": "x"}]}


def test_build_result_without_bocha_sum():
    """无 bocha_sum / 空列表 / None：metadata 均为空 dict。"""
    assert _build_result("text").metadata == {}
    assert _build_result("text", bocha_sum=[]).metadata == {}
    assert _build_result("text", bocha_sum=None).metadata == {}
