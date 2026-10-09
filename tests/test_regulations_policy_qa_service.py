"""PolicyQAService 单测：blocking 响应字段 / 空应答缺口记录 / SSE 流式事件序列。

graph 用 monkeypatch 替换（ainvoke / astream_events / get_state）；Langfuse 用
未启用 no-op；KBClient 用 MagicMock —— 全程不触外部服务。
"""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.regulations.services.policy_qa_service as pqa_module
from app.regulations.schemas import GeneralQAResponse, RetrieverResource
from app.regulations.services.policy_qa_service import PolicyQAService


# ---- mock 基础设施 ----

_MODEL_CFG = {
    "provider": "openai",
    "model_name": "test-model",
    "base_url": "http://llm.test/v1",
    "api_key": "sk-test",
    "parameters": {"temperature": 0.3, "timeout": 30},
}


class _NoopLangfuse:
    """未启用状态的 Langfuse 客户端：全部 no-op，仅记录 score 便于断言。"""

    def __init__(self):
        self.enabled = False
        self.scores = []

    @asynccontextmanager
    async def start_trace(self, **kwargs):
        yield None

    @asynccontextmanager
    async def start_span(self, name, input=None):
        yield None

    def score(self, trace_id, name, value, comment=""):
        self.scores.append({"trace_id": trace_id, "name": name, "value": value})


class _FakeGraph:
    """blocking 图替身：记录 ainvoke 入参，返回预设 final state。"""

    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error
        self.calls = []

    async def ainvoke(self, state, config=None):
        self.calls.append({"state": dict(state), "config": config})
        if self.error:
            raise self.error
        return dict(self.result)


class _FakeAStream:
    def __init__(self, events, error=None):
        self._iter = iter(events)
        self._error = error

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._error:
            raise self._error
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        pass


class _FakeStreamGraph:
    """streaming 图替身：吐预设事件序列，get_state 返回最终 state。"""

    def __init__(self, events=None, final_values=None, error=None):
        self._events = events or []
        self._final = final_values or {}
        self._error = error
        self.astream_calls = []

    def astream_events(self, state, config=None, version="v2"):
        self.astream_calls.append(dict(state))
        return _FakeAStream(self._events, error=self._error)

    def get_state(self, config):
        return SimpleNamespace(values=dict(self._final))


def _docs():
    return [{
        "position": 1, "dataset_id": "kb1", "dataset_name": "制度库",
        "document_id": "d1", "document_name": "差旅报销办法", "segment_id": "s1",
        "content": "报销标准…", "score": 0.9, "page_start": 1, "page_end": 2,
    }]


def _make_service(monkeypatch, graph=None):
    gap_service = MagicMock()
    gap_service.record_gap = AsyncMock()
    svc = PolicyQAService(dict(_MODEL_CFG), MagicMock(), gap_service)
    if graph is not None:
        monkeypatch.setattr(svc, "graph", graph)
    langfuse = _NoopLangfuse()
    monkeypatch.setattr(pqa_module, "get_langfuse", lambda: langfuse)
    return svc, gap_service, langfuse


async def _drain_pending(svc):
    """等待后台任务（缺口记录等）完成。"""
    if svc._pending_tasks:
        await asyncio.gather(*list(svc._pending_tasks), return_exceptions=True)


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """解析 SSE 文本为 (event_type, data) 列表。"""
    events = []
    cur_type = None
    for line in body.splitlines():
        if line.startswith("event: "):
            cur_type = line[len("event: "):].strip()
        elif line.startswith("data: "):
            events.append((cur_type, json.loads(line[len("data: "):])))
    return events


# ════════════════════════════════════════════════════════════
# blocking 模式
# ════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_blocking_returns_response_fields(monkeypatch):
    """graph 返回 answer/retrieved_docs → GeneralQAResponse 字段完整。"""
    graph = _FakeGraph(result={"answer": "报销标准如下", "retrieved_docs": _docs()})
    svc, gap, _ = _make_service(monkeypatch, graph)
    serial = "oiapolyqa" + "a" * 32

    resp = await svc.run_general_qa(
        question="差旅报销标准？", kb_ids=["kb1"], serial=serial,
        user_id="u1", enable_query_understanding=False,
    )

    assert isinstance(resp, GeneralQAResponse)
    assert resp.answer == "报销标准如下"
    assert len(resp.citations) == 1
    assert isinstance(resp.citations[0], RetrieverResource)
    assert resp.citations[0].document_name == "差旅报销办法"
    assert resp.app_serial_number == serial
    assert resp.model == "test-model"
    assert resp.created_at > 0

    # graph 收到正确初始状态与 thread_id
    state = graph.calls[0]["state"]
    assert state["query"] == "差旅报销标准？"
    assert state["kb_ids"] == ["kb1"]
    assert state["enable_query_understanding"] is False
    assert graph.calls[0]["config"]["configurable"]["thread_id"] == serial

    # 正常回答不落缺口
    await _drain_pending(svc)
    gap.record_gap.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocking_generates_serial_when_missing(monkeypatch):
    """serial 缺省 → 生成 9 位组件前缀 + 时间戳 + 随机后缀的流水号。"""
    graph = _FakeGraph(result={"answer": "ok", "retrieved_docs": _docs()})
    svc, _, _ = _make_service(monkeypatch, graph)

    resp = await svc.run_general_qa(question="q", kb_ids=["kb1"])

    assert resp.app_serial_number.startswith("oiapolyqa")
    assert len(resp.app_serial_number) == 9 + 13 + 16


@pytest.mark.asyncio
async def test_blocking_empty_answer_records_gap(monkeypatch):
    """空应答（answer 为空）→ result_type=empty → 后台记录缺口（kb_ids[0], question）。"""
    graph = _FakeGraph(result={"answer": "", "retrieved_docs": _docs()})
    svc, gap, langfuse = _make_service(monkeypatch, graph)

    await svc.run_general_qa(question="没答案的问题", kb_ids=["kb1", "kb2"])
    await _drain_pending(svc)

    gap.record_gap.assert_awaited_once_with("kb1", "没答案的问题")
    assert langfuse.scores[-1] == {
        "trace_id": langfuse.scores[-1]["trace_id"],
        "name": "empty_answer", "value": True,
    }


@pytest.mark.asyncio
async def test_blocking_empty_retrieval_placeholder_is_empty(monkeypatch):
    """空检索占位回答（含「无法回答」句式）→ 判定 empty → 落缺口。"""
    graph = _FakeGraph(result={
        "answer": "未检索到相关内容，无法回答。", "retrieved_docs": [],
    })
    svc, gap, langfuse = _make_service(monkeypatch, graph)

    await svc.run_general_qa(question="查不到的问题", kb_ids=["kb1"])
    await _drain_pending(svc)

    gap.record_gap.assert_awaited_once_with("kb1", "查不到的问题")
    assert langfuse.scores[-1]["name"] == "empty_answer"
    assert langfuse.scores[-1]["value"] is True


@pytest.mark.asyncio
async def test_blocking_normal_answer_no_gap(monkeypatch):
    """正常回答（有 docs 且无拒答句式）→ 不落缺口，score empty_answer=False。"""
    graph = _FakeGraph(result={"answer": "依据第 3 条…", "retrieved_docs": _docs()})
    svc, gap, langfuse = _make_service(monkeypatch, graph)

    await svc.run_general_qa(question="正常问题", kb_ids=["kb1"])
    await _drain_pending(svc)

    gap.record_gap.assert_not_awaited()
    assert langfuse.scores[-1]["value"] is False


@pytest.mark.asyncio
async def test_blocking_refuse_no_gap(monkeypatch):
    """领域拒答（refuse=True）→ result_type=refused，不落缺口。"""
    graph = _FakeGraph(result={
        "answer": "我只能回答公司制度相关问题。",
        "retrieved_docs": [],
        "refuse": True,
    })
    svc, gap, langfuse = _make_service(monkeypatch, graph)

    await svc.run_general_qa(question="今天天气怎么样", kb_ids=["kb1"])
    await _drain_pending(svc)

    gap.record_gap.assert_not_awaited()
    assert langfuse.scores[-1]["value"] is False


@pytest.mark.asyncio
async def test_blocking_graph_error_propagates(monkeypatch):
    """graph 异常 → 原样上抛（路由层负责转 502/500）。"""
    graph = _FakeGraph(error=RuntimeError("graph exploded"))
    svc, _, _ = _make_service(monkeypatch, graph)

    with pytest.raises(RuntimeError, match="graph exploded"):
        await svc.run_general_qa(question="q", kb_ids=["kb1"])


# ════════════════════════════════════════════════════════════
# streaming 模式
# ════════════════════════════════════════════════════════════

def _stream_events():
    """模拟一次正常流式问答的 LangGraph 事件序列。"""
    return [
        {"event": "on_chain_start", "name": "rewrite",
         "data": {"input": {"query": "差旅报销标准？"}}},
        {"event": "on_chain_end", "name": "rewrite", "data": {"output": {}}},
        {"event": "on_chain_start", "name": "retrieve",
         "data": {"input": {"kb_ids": ["kb1"]}}},
        {"event": "on_chain_end", "name": "retrieve",
         "data": {"output": {"retrieved_docs": _docs()}}},
        {"event": "on_chat_model_stream", "name": "llm",
         "metadata": {"langgraph_node": "generate"},
         "data": {"chunk": SimpleNamespace(content="报销", additional_kwargs={})}},
        {"event": "on_chat_model_stream", "name": "llm",
         "metadata": {"langgraph_node": "generate"},
         "data": {"chunk": SimpleNamespace(content="标准如下", additional_kwargs={})}},
        {"event": "on_chat_model_end", "name": "llm",
         "data": {"output": {"usage_metadata": {"input_tokens": 10, "output_tokens": 5}}}},
        {"event": "on_chain_start", "name": "generate", "data": {}},
        {"event": "on_chain_end", "name": "generate", "data": {"output": {}}},
    ]


async def _consume(resp) -> str:
    body = ""
    async for chunk in resp.body_iterator:
        body += chunk
    return body


@pytest.mark.asyncio
async def test_streaming_event_sequence(monkeypatch):
    """SSE 序列：message_start 开头 → stage → citation → content_block_delta →
    message_delta → message_stop 结尾。"""
    graph = _FakeStreamGraph(
        events=_stream_events(),
        final_values={"answer": "报销标准如下", "retrieved_docs": _docs()},
    )
    svc, gap, _ = _make_service(monkeypatch, graph)
    serial = "oiapolyqa" + "b" * 32

    resp = await svc.run_general_qa_stream(
        question="差旅报销标准？", kb_ids=["kb1"], serial=serial,
    )
    body = await _consume(resp)
    events = _parse_sse(body)
    types = [t for t, _ in events]

    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    assert types.count("message_start") == 1
    assert types.count("message_stop") == 1

    # message_start 复用调用方流水号
    ms = events[0][1]
    assert ms["message_id"] == serial
    assert ms["conversation_id"] == serial
    assert ms["model"] == "test-model"

    # 阶段事件：started + completed
    assert "stage" in types
    started = [d for t, d in events if t == "stage" and d.get("status") == "started"]
    assert {d["name"] for d in started} == {"rewrite", "retrieve", "generate"}

    # citation 事件携带检索结果
    citation = next(d for t, d in events if t == "citation")
    assert citation["resources"][0]["document_name"] == "差旅报销办法"

    # 正文 token 透传（仅 generate 节点）
    texts = [d["delta"]["text"] for t, d in events if t == "content_block_delta"]
    assert "报销标准如下" == "".join(texts)

    # message_delta 携带 stop_reason 与 usage
    md = next(d for t, d in events if t == "message_delta")
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"]["prompt_tokens"] == 10
    assert md["usage"]["completion_tokens"] == 5

    # 正常流式不落缺口
    await _drain_pending(svc)
    gap.record_gap.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_ignores_non_generate_model_tokens(monkeypatch):
    """rewrite 等节点的 on_chat_model_stream 不透传为正文。"""
    events = [
        {"event": "on_chat_model_stream", "name": "llm",
         "metadata": {"langgraph_node": "rewrite"},
         "data": {"chunk": SimpleNamespace(content="改写文本", additional_kwargs={})}},
        {"event": "on_chat_model_stream", "name": "llm",
         "metadata": {"langgraph_node": "generate"},
         "data": {"chunk": SimpleNamespace(content="正文", additional_kwargs={})}},
    ]
    graph = _FakeStreamGraph(events=events, final_values={"answer": "正文", "retrieved_docs": []})
    svc, _, _ = _make_service(monkeypatch, graph)

    resp = await svc.run_general_qa_stream(question="q", kb_ids=["kb1"])
    body = await _consume(resp)
    events = _parse_sse(body)

    texts = [d["delta"]["text"] for t, d in events if t == "content_block_delta"]
    assert texts == ["正文"]


@pytest.mark.asyncio
async def test_streaming_empty_retrieval_supplements_answer(monkeypatch):
    """空检索（generate 短路、无 model stream 事件）→ 补发 content_block_delta
    携带占位回答，不能无正文结束；并落知识缺口。"""
    graph = _FakeStreamGraph(
        events=[],
        final_values={"answer": "未检索到相关内容，无法回答。", "retrieved_docs": []},
    )
    svc, gap, langfuse = _make_service(monkeypatch, graph)

    resp = await svc.run_general_qa_stream(question="空检索问题", kb_ids=["kb1"])
    body = await _consume(resp)
    events = _parse_sse(body)
    types = [t for t, _ in events]

    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    texts = [d["delta"]["text"] for t, d in events if t == "content_block_delta"]
    assert texts, "空检索流式也应补发 content_block_delta"
    assert "未检索到相关内容" in "".join(texts)
    # 无 citation 事件
    assert "citation" not in types

    await _drain_pending(svc)
    gap.record_gap.assert_awaited_once_with("kb1", "空检索问题")
    assert langfuse.scores[-1]["value"] is True


@pytest.mark.asyncio
async def test_streaming_message_start_serial_generated(monkeypatch):
    """serial 缺省时自动生成并写入 message_start。"""
    graph = _FakeStreamGraph(
        events=_stream_events(),
        final_values={"answer": "ok", "retrieved_docs": _docs()},
    )
    svc, _, _ = _make_service(monkeypatch, graph)

    resp = await svc.run_general_qa_stream(question="q", kb_ids=["kb1"])
    body = await _consume(resp)
    events = _parse_sse(body)

    ms = events[0][1]
    assert ms["message_id"].startswith("oiapolyqa")


@pytest.mark.asyncio
async def test_streaming_error_emits_error_event(monkeypatch):
    """graph 事件流异常 → error 事件（错误码分类）+ message_stop 收尾。"""
    graph = _FakeStreamGraph(error=RuntimeError("知识库检索失败: kb down"))
    svc, _, _ = _make_service(monkeypatch, graph)

    resp = await svc.run_general_qa_stream(question="q", kb_ids=["kb1"])
    body = await _consume(resp)
    events = _parse_sse(body)
    types = [t for t, _ in events]

    assert types[0] == "message_start"
    assert "error" in types
    assert types[-1] == "message_stop"

    err = next(d for t, d in events if t == "error")
    assert err["code"] == "retrieval_error"  # 消息含「知识库/检索」关键词
    assert err["message"]
