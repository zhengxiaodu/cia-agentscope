"""KBClient 单测：字段映射 / 业务错误 / 网络与 5xx 重试 / 4xx 不重试 / 多路合并去重。

httpx 用 MockTransport 注入响应与异常；重试等待（asyncio.sleep 1s）替换为
零耗时 stub，避免用例拖慢。
"""
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import httpx
import pytest

import app.regulations.services.kb_client as kb_client_module
from app.regulations.services.kb_client import KBClient, KBClientError


# ---- mock 基础设施 ----

def _no_langfuse(monkeypatch):
    """屏蔽 retrieval span 埋点（未启用状态 no-op），保证测试不触网。"""
    class _NoopLangfuse:
        @asynccontextmanager
        async def start_span(self, name, input=None):
            yield None

    monkeypatch.setattr(kb_client_module, "get_langfuse", lambda: _NoopLangfuse())


def _fast_sleep(monkeypatch):
    """重试间隔 1s → 0s（asyncio.sleep 为模块内引用，直接替换为立即返回）。"""
    async def _sleep(_delay):
        return None

    monkeypatch.setattr(kb_client_module.asyncio, "sleep", _sleep)


class _ScriptedHandler:
    """按脚本依次返回响应/抛异常的 MockTransport handler，并统计请求数。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        step = self.script.pop(0)
        if callable(step):
            step = step(request)
        if isinstance(step, Exception):
            raise step
        return step


class _ScriptedTransport(httpx.MockTransport):
    """带脚本与请求计数的 MockTransport。"""

    def __init__(self, script):
        self._handler = _ScriptedHandler(script)
        super().__init__(self._handler)

    @property
    def requests(self):
        return self._handler.requests


def _ok_response(results=None, request_id="req-1"):
    return httpx.Response(200, json={
        "code": 0,
        "request_id": request_id,
        "data": {
            "results": results or [],
            "latency_ms": {"embedding": 5, "search": 10, "rerank": 20, "total": 35},
        },
    })


def _sample_kb_results():
    return [{
        "kb_id": "kb1", "kb_name": "制度库",
        "doc_id": "d1", "doc_name": "差旅报销办法",
        "chunk_id": "c1", "content": "缴纳比例 20%", "score": 0.9,
        "page_start": 1, "page_end": 2, "element_ids": ["e1"], "chunk_type": "article",
    }]


def _make_client(script):
    """构造 KBClient 并替换底层 httpx client 为脚本化 MockTransport。"""
    kb = KBClient()
    transport = _ScriptedTransport(script)
    kb.client = httpx.AsyncClient(
        base_url=kb.base_url,
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        transport=transport,
    )
    return kb, transport


def _conn_error(request):
    return httpx.ConnectError("connection refused", request=request)


# ---- 字段映射 ----

@pytest.mark.asyncio
async def test_retrieve_maps_fields():
    """KB API 字段 → 内部统一格式（Dify 字段名），position 按序编号。"""
    kb, _ = _make_client([_ok_response(_sample_kb_results())])
    try:
        results = await kb.retrieve(kb_ids=["kb1"], query="差旅报销标准")
    finally:
        await kb.close()

    assert len(results) == 1
    r = results[0]
    assert r["position"] == 1
    assert r["dataset_id"] == "kb1"
    assert r["dataset_name"] == "制度库"
    assert r["document_id"] == "d1"
    assert r["document_name"] == "差旅报销办法"
    assert r["segment_id"] == "c1"
    assert r["content"] == "缴纳比例 20%"
    assert r["score"] == 0.9
    assert r["page_start"] == 1
    assert r["page_end"] == 2
    assert r["element_ids"] == ["e1"]
    assert r["chunk_type"] == "article"


@pytest.mark.asyncio
async def test_retrieve_position_enumerates():
    """多条结果 position 从 1 连续编号。"""
    rows = _sample_kb_results() + [dict(_sample_kb_results()[0], chunk_id="c2", score=0.5)]
    kb, _ = _make_client([_ok_response(rows)])
    try:
        results = await kb.retrieve(kb_ids=["kb1"], query="q")
    finally:
        await kb.close()
    assert [r["position"] for r in results] == [1, 2]


@pytest.mark.asyncio
async def test_retrieve_payload_and_headers():
    """请求体含 kb_ids/query/top_k/rerank_top_k（上限 20）/hybrid；headers 带内部 Bearer。"""
    kb = KBClient()
    kb.token = "test-kb-internal-token"
    captured = {}

    def handler(request):
        import json as _json
        captured["headers"] = dict(request.headers)
        captured["payload"] = _json.loads(request.content)
        return _ok_response([])

    kb.client = httpx.AsyncClient(
        base_url=kb.base_url, timeout=httpx.Timeout(10.0), transport=httpx.MockTransport(handler),
    )
    try:
        await kb.retrieve(
            kb_ids=["kb1", "kb2"], query="报销", top_k=30, rerank_top_k=50,
            score_threshold=0.3, user_id="usr_1",
        )
    finally:
        await kb.close()

    assert captured["headers"]["authorization"] == "Bearer test-kb-internal-token"
    assert "x-request-id" in captured["headers"]
    payload = captured["payload"]
    assert payload["kb_ids"] == ["kb1", "kb2"]
    assert payload["query"] == "报销"
    assert payload["top_k"] == 30
    assert payload["rerank_top_k"] == 20  # KB 契约上限
    assert payload["score_threshold"] == 0.3
    assert payload["retrieval_mode"] == "hybrid"
    assert payload["user_id"] == "usr_1"


@pytest.mark.asyncio
async def test_retrieve_omits_empty_user_id():
    """user_id 为空时不写入 payload。"""
    kb = KBClient()
    captured = {}

    def handler(request):
        import json as _json
        captured["payload"] = _json.loads(request.content)
        return _ok_response([])

    kb.client = httpx.AsyncClient(
        base_url=kb.base_url, timeout=httpx.Timeout(10.0), transport=httpx.MockTransport(handler),
    )
    try:
        await kb.retrieve(kb_ids=["kb1"], query="q")
    finally:
        await kb.close()
    assert "user_id" not in captured["payload"]


# ---- 业务错误 ----

@pytest.mark.asyncio
async def test_retrieve_business_error_raises():
    """code != 0 → KBClientError（code=retrieval_error，消息含 request_id）。"""
    body = httpx.Response(200, json={
        "code": 1001, "message": "kb not found", "request_id": "req-42",
    })
    kb, _ = _make_client([body])
    try:
        with pytest.raises(KBClientError) as ei:
            await kb.retrieve(kb_ids=["kb1"], query="q")
    finally:
        await kb.close()

    err = ei.value
    assert err.code == "retrieval_error"
    assert err.request_id == "req-42"
    assert "req-42" in err.message
    assert "kb not found" in err.message


# ---- 重试 ----

@pytest.mark.asyncio
async def test_retrieve_retries_request_error_then_succeeds(monkeypatch):
    """网络错误（RequestError）重试：连续 2 次失败后第 3 次成功。"""
    _fast_sleep(monkeypatch)
    ok = _ok_response(_sample_kb_results())
    kb, transport = _make_client([
        _conn_error, _conn_error, ok,
    ])
    try:
        results = await kb.retrieve(kb_ids=["kb1"], query="q", max_retries=2)
    finally:
        await kb.close()

    assert len(transport.requests) == 3
    assert results[0]["document_id"] == "d1"


@pytest.mark.asyncio
async def test_retrieve_retries_5xx_then_succeeds(monkeypatch):
    """5xx 视为瞬时错误重试：500、502 后 200 成功。"""
    _fast_sleep(monkeypatch)
    kb, transport = _make_client([
        httpx.Response(500, json={"code": -1}),
        httpx.Response(502, json={"code": -1}),
        _ok_response(_sample_kb_results()),
    ])
    try:
        results = await kb.retrieve(kb_ids=["kb1"], query="q", max_retries=2)
    finally:
        await kb.close()

    assert len(transport.requests) == 3
    assert results[0]["segment_id"] == "c1"


@pytest.mark.parametrize("status", [400, 401, 404, 422])
@pytest.mark.asyncio
async def test_retrieve_4xx_no_retry_raises(status):
    """4xx 确定性错误：不重试直接抛 KBClientError。"""
    kb, transport = _make_client([httpx.Response(status, json={"code": -1})])
    try:
        with pytest.raises(KBClientError) as ei:
            await kb.retrieve(kb_ids=["kb1"], query="q", max_retries=2)
    finally:
        await kb.close()

    assert len(transport.requests) == 1
    assert f"{status}" in str(ei.value) or "知识库检索失败" in str(ei.value)


@pytest.mark.asyncio
async def test_retrieve_exhausts_retries_raises(monkeypatch):
    """连续网络错误且重试用尽 → KBClientError，消息注明已重试次数。"""
    _fast_sleep(monkeypatch)
    kb, transport = _make_client([_conn_error, _conn_error, _conn_error])
    try:
        with pytest.raises(KBClientError) as ei:
            await kb.retrieve(kb_ids=["kb1"], query="q", max_retries=2)
    finally:
        await kb.close()

    assert len(transport.requests) == 3  # 1 次原始 + 2 次重试
    assert "已重试 2 次" in str(ei.value)


# ---- 多路检索（retrieve_multi）----

def _row(segment_id, score):
    return {
        "segment_id": segment_id, "document_id": "d", "score": score,
        "content": f"c-{segment_id}", "dataset_id": "kb", "dataset_name": "制度库",
    }


def _fake_retrieve_once(monkeypatch, kb, mapping):
    """替换 _retrieve_once：按 query 返回预设结果或抛异常。"""
    async def _fake(kb_ids, query, top_k, rerank_top_k, score_threshold, user_id, max_retries):
        result = mapping.get(query)
        if isinstance(result, Exception):
            raise result
        return result or []

    monkeypatch.setattr(kb, "_retrieve_once", _fake)


@pytest.mark.asyncio
async def test_retrieve_multi_merges_and_dedups(monkeypatch):
    """多 query 结果合并、按 segment_id 去重（保留最高分）、rerank_top_k 截断、position 重排。"""
    _no_langfuse(monkeypatch)
    kb = KBClient()
    _fake_retrieve_once(monkeypatch, kb, {
        "q1": [_row("s1", 0.9), _row("s2", 0.5)],
        "q2": [_row("s2", 0.8), _row("s3", 0.6)],
        "q3": [_row("s4", 0.1)],
    })
    try:
        out = await kb.retrieve_multi(
            kb_ids=["kb"], queries=["q1", "q2", "q3"], rerank_top_k=3,
        )
    finally:
        await kb.close()

    # s2 两路中取高分 0.8；按分数降序取 top3（s4=0.1 被截断）；position 重排
    assert [r["segment_id"] for r in out] == ["s1", "s2", "s3"]
    assert out[1]["score"] == 0.8
    assert [r["position"] for r in out] == [1, 2, 3]


@pytest.mark.asyncio
async def test_retrieve_multi_partial_failure_keeps_results(monkeypatch):
    """部分 query 失败：保留成功路结果，不抛错。"""
    _no_langfuse(monkeypatch)
    kb = KBClient()
    _fake_retrieve_once(monkeypatch, kb, {
        "good": [_row("s1", 0.9)],
        "bad": RuntimeError("boom"),
    })
    try:
        out = await kb.retrieve_multi(kb_ids=["kb"], queries=["good", "bad"], rerank_top_k=5)
    finally:
        await kb.close()
    assert [r["segment_id"] for r in out] == ["s1"]


@pytest.mark.asyncio
async def test_retrieve_multi_all_fail_raises_first_error(monkeypatch):
    """全部失败：抛 KBClientError，包装第一个异常。"""
    _no_langfuse(monkeypatch)
    kb = KBClient()
    _fake_retrieve_once(monkeypatch, kb, {
        "q1": RuntimeError("first boom"),
        "q2": RuntimeError("second boom"),
    })
    try:
        with pytest.raises(KBClientError) as ei:
            await kb.retrieve_multi(kb_ids=["kb"], queries=["q1", "q2"], rerank_top_k=5)
    finally:
        await kb.close()
    assert "first boom" in str(ei.value)


@pytest.mark.asyncio
async def test_retrieve_multi_empty_queries_returns_empty(monkeypatch):
    """queries 为空 → 直接返回空列表，不发请求。"""
    _no_langfuse(monkeypatch)
    kb = KBClient()
    called = MagicMock()

    async def _should_not_call(*args, **kwargs):
        called()

    monkeypatch.setattr(kb, "_retrieve_once", _should_not_call)
    try:
        out = await kb.retrieve_multi(kb_ids=["kb"], queries=[], rerank_top_k=5)
    finally:
        await kb.close()
    assert out == []
    called.assert_not_called()
