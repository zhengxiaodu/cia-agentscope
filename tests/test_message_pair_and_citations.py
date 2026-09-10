"""会话消息配对（message_pair_id）与制度问答引用（citations）持久化测试。

覆盖：
1. SessionDAO.append_messages：消息 dict 带 message_pair_id/citations 时
   INSERT SQL 含新列与参数；user/assistant 共享同值；citations 为 list 时
   json.dumps、空时写 None；返回 {"user_message_id", "assistant_message_id"}
2. SessionDAO.load_messages：旧记录（message_pair_id=None、citations=None/JSON
   字符串）映射为 None / []
3. chat_service.generate_response 集成：编排流 policy_qa_citations 多条事件
   累积合并后随 assistant 消息落库；非制度问答轮 citations 为 None；
   中断（cancel_event）finally 落库同样携带 pair_id 与已捕获 citations
"""
import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_service as chat_service_module
from app.dao.mysql_session_dao import SessionDAO
from app.services.chat_service import generate_response


# ---------------------------------------------------------------------------
# 假 pool / conn / cursor（风格对齐 test_upload_query.py）
# ---------------------------------------------------------------------------

class _FakeCursor:
    """按 SQL 类型返回 fetchone 结果；INSERT INTO messages 时 lastrowid 自增。"""

    def __init__(self, rows=None, session_row=None, count_row=None):
        self._rows = rows or []
        self._session_row = session_row
        self._count_row = count_row if count_row is not None else {"cnt": 0}
        self.executed = []
        self._next_id = 100
        self.lastrowid = 0

    async def execute(self, sql, args=None):
        self.executed.append((sql, args))
        if sql.startswith("INSERT INTO messages"):
            self._next_id += 1
            self.lastrowid = self._next_id

    async def fetchone(self):
        sql = self.executed[-1][0] if self.executed else ""
        if "COUNT(*)" in sql:
            return self._count_row
        if sql.startswith("SELECT name, agent_ids FROM sessions"):
            return self._session_row
        return None

    async def fetchall(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor_obj):
        self.cursor_obj = cursor_obj
        self.committed = False

    def cursor(self, cursor_class=None):
        return self.cursor_obj

    async def begin(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, cursor_obj):
        self.conn = _FakeConn(cursor_obj)

    def acquire(self):
        return self.conn


def _make_dao(session_row=None, count_row=None, rows=None):
    cur = _FakeCursor(rows=rows, session_row=session_row, count_row=count_row)
    return SessionDAO(_FakePool(cur)), cur


# ---------------------------------------------------------------------------
# SessionDAO.append_messages：message_pair_id / citations 写库
# ---------------------------------------------------------------------------

_CITATIONS = [{"title": "差旅报销办法", "url": "http://x/1"}, {"title": "权限清单", "url": "http://x/2"}]


@pytest.mark.asyncio
async def test_append_messages_writes_pair_id_and_citations():
    """user/assistant 消息共享同值 message_pair_id；assistant citations
    （非空 list）json.dumps 写库，user 消息 citations 写 None。"""
    dao, cur = _make_dao(
        session_row={"name": "n", "agent_ids": None},
        count_row={"cnt": 2},
    )
    messages = [
        {"role": "user", "content": "报销制度", "user_id": "u1",
         "message_pair_id": "pair-1"},
        {"role": "assistant", "content": "见引用", "user_id": "u1",
         "message_pair_id": "pair-1", "citations": _CITATIONS},
    ]

    result = await dao.append_messages("s1", "u1", messages)

    msg_inserts = [
        (sql, args) for sql, args in cur.executed
        if sql.startswith("INSERT INTO messages")
    ]
    assert len(msg_inserts) == 2
    for sql, _ in msg_inserts:
        # SQL 列清单含三个扩展列，占位符扩到 11 个
        assert "message_pair_id, citations, bocha_sum" in sql
        assert sql.count("%s") == 11

    user_sql, user_args = msg_inserts[0]
    asst_sql, asst_args = msg_inserts[1]
    # user/assistant 共享同一 message_pair_id（倒数第三个参数）
    assert user_args[-3] == "pair-1"
    assert asst_args[-3] == "pair-1"
    # user 消息不带 citations/bocha_sum → None；assistant 序列化写库
    assert user_args[-2] is None
    assert asst_args[-2] == json.dumps(_CITATIONS, ensure_ascii=False)
    assert user_args[-1] is None
    assert asst_args[-1] is None
    # 事务提交
    assert dao.pool.conn.committed


@pytest.mark.asyncio
async def test_append_messages_empty_citations_written_as_none():
    """citations 为空 list 或 None 时均写 NULL（非空才序列化）。"""
    dao, cur = _make_dao(
        session_row={"name": "n", "agent_ids": None},
        count_row={"cnt": 2},
    )
    messages = [
        {"role": "assistant", "content": "答", "message_pair_id": "p",
         "citations": []},
        {"role": "assistant", "content": "答2", "message_pair_id": "p",
         "citations": None},
    ]

    await dao.append_messages("s1", "u1", messages)

    msg_inserts = [
        (sql, args) for sql, args in cur.executed
        if sql.startswith("INSERT INTO messages")
    ]
    assert len(msg_inserts) == 2
    assert msg_inserts[0][1][-2] is None
    assert msg_inserts[1][1][-2] is None


@pytest.mark.asyncio
async def test_append_messages_writes_bocha_sum():
    """非空 bocha_sum（list）json.dumps 写库；空列表/None/缺键均写 NULL。

    注：DAO 层对 bocha_sum 角色无关（与 citations 一致），
    实际链路中仅 assistant 消息携带（见 chat_service._persist_conversation_history）。
    """
    dao, cur = _make_dao(
        session_row={"name": "n", "agent_ids": None},
        count_row={"cnt": 2},
    )
    messages = [
        {"role": "user", "content": "搜新闻", "message_pair_id": "p"},
        {"role": "assistant", "content": "基于搜索的回答", "message_pair_id": "p",
         "bocha_sum": _BOCHA_SUM},
        {"role": "assistant", "content": "无来源回答", "message_pair_id": "p",
         "bocha_sum": []},
        {"role": "assistant", "content": "None 回答", "message_pair_id": "p",
         "bocha_sum": None},
    ]

    await dao.append_messages("s1", "u1", messages)

    msg_inserts = [
        (sql, args) for sql, args in cur.executed
        if sql.startswith("INSERT INTO messages")
    ]
    assert len(msg_inserts) == 4
    # user 消息（不带 bocha_sum 键）写 None
    assert msg_inserts[0][1][-1] is None
    # assistant 非空 bocha_sum 序列化写库
    assert msg_inserts[1][1][-1] == json.dumps(_BOCHA_SUM, ensure_ascii=False)
    # 空列表与 None 均写 NULL
    assert msg_inserts[2][1][-1] is None
    assert msg_inserts[3][1][-1] is None


@pytest.mark.asyncio
async def test_append_messages_returns_dual_ids():
    """返回 dict：user/assistant 各自的自增 id（来自 lastrowid）。"""
    dao, cur = _make_dao(
        session_row={"name": "n", "agent_ids": None},
        count_row={"cnt": 2},
    )
    messages = [
        {"role": "user", "content": "问", "message_pair_id": "pair-1"},
        {"role": "assistant", "content": "答", "message_pair_id": "pair-1"},
    ]

    result = await dao.append_messages("s1", "u1", messages)

    assert result == {"user_message_id": 101, "assistant_message_id": 102}


@pytest.mark.asyncio
async def test_append_messages_assistant_only_returns_none_user_id():
    """只有 assistant 消息时 user_message_id 为 None（dict 双键仍存在）。"""
    dao, cur = _make_dao(
        session_row={"name": "n", "agent_ids": None},
        count_row={"cnt": 1},
    )
    messages = [{"role": "assistant", "content": "答", "message_pair_id": "p"}]

    result = await dao.append_messages("s1", "u1", messages)

    assert result == {"user_message_id": None, "assistant_message_id": 101}


# ---------------------------------------------------------------------------
# SessionDAO.load_messages：新列映射（旧记录兼容）
# ---------------------------------------------------------------------------

_BOCHA_SUM = [{"name": "网页标题", "url": "https://example.com", "snippet": "片段"}]


@pytest.mark.asyncio
async def test_load_messages_maps_pair_id_and_citations():
    """旧记录 message_pair_id=None、citations/bocha_sum=None/JSON 字符串 →
    None / []；JSON 字符串解析为 list；已解析 list 原样透出。"""
    rows = [
        # 旧 user 记录：扩展列均为 NULL
        {"role": "user", "content": "问", "timestamp": datetime(2026, 1, 1, 12, 0, 0),
         "agent_ids": None, "user_id": "u1", "success": 1, "tokens": 3,
         "message_pair_id": None, "citations": None, "bocha_sum": None},
        # 新 assistant 记录：citations/bocha_sum 为 JSON 字符串（aiomysql 常见形态）
        {"role": "assistant", "content": "答", "timestamp": datetime(2026, 1, 1, 12, 0, 1),
         "agent_ids": '["a1"]', "user_id": "u1", "success": 1, "tokens": 5,
         "message_pair_id": "pair-1",
         "citations": json.dumps(_CITATIONS, ensure_ascii=False),
         "bocha_sum": json.dumps(_BOCHA_SUM, ensure_ascii=False)},
        # citations/bocha_sum 已是 list（驱动解析后形态）
        {"role": "assistant", "content": "答2", "timestamp": datetime(2026, 1, 1, 12, 0, 2),
         "agent_ids": None, "user_id": "u1", "success": 1, "tokens": 5,
         "message_pair_id": "pair-2", "citations": _CITATIONS,
         "bocha_sum": _BOCHA_SUM},
    ]
    dao, cur = _make_dao(rows=rows)

    result = await dao.load_messages("s1")

    sql, args = cur.executed[0]
    assert "message_pair_id, citations, bocha_sum" in sql
    assert args == ("s1",)

    assert result[0]["message_pair_id"] is None
    assert result[0]["citations"] == []
    assert result[0]["bocha_sum"] == []
    assert result[1]["message_pair_id"] == "pair-1"
    assert result[1]["citations"] == _CITATIONS
    assert result[1]["bocha_sum"] == _BOCHA_SUM
    assert result[2]["message_pair_id"] == "pair-2"
    assert result[2]["citations"] == _CITATIONS
    assert result[2]["bocha_sum"] == _BOCHA_SUM


# ---------------------------------------------------------------------------
# chat_service.generate_response：citations 捕获与配对落库（mock 集成）
# ---------------------------------------------------------------------------

_SAFE_SENS = {
    "blocked": False, "reason": "", "hit_sources": [],
    "sensitive_words": [], "source": "server", "raw": {},
}


def _patch_sensitive(monkeypatch):
    """屏蔽真实敏感检测外呼（.env 可能配置了真实服务地址）。"""
    async def fake_dict(text, stage="input"):
        return dict(_SAFE_SENS)

    async def fake_check(text, stage="output"):
        return dict(_SAFE_SENS)

    monkeypatch.setattr(chat_service_module, "dict_check_sensitive", fake_dict)
    monkeypatch.setattr(chat_service_module, "check_sensitive", fake_check)


def _patch_recommended_questions(monkeypatch):
    """屏蔽推荐问题真实 LLM 外呼（mock orchestrator 的 intent client 为 MagicMock）。"""
    async def fake_emit_rq(*args, **kwargs):
        yield "data: {\"type\": \"recommended_questions\", \"questions\": [\"q1\"]}\n\n"

    monkeypatch.setattr(chat_service_module, "_emit_recommended_questions", fake_emit_rq)


def _make_orch(events, run_body=None):
    """orchestrator mock：run 为依次 yield 指定事件的 async 生成器。"""
    orch = MagicMock()

    if run_body is not None:
        fake_run = run_body
    else:
        async def fake_run(*args, **kwargs):
            for ev in events:
                yield ev

    orch.run = fake_run
    return orch


def _make_session_service():
    svc = MagicMock()
    svc.load_messages = AsyncMock(return_value=[])
    svc.append_messages = AsyncMock(return_value={
        "user_message_id": 101, "assistant_message_id": 102,
    })
    svc.save_latest_trace_id = AsyncMock()
    svc.append_session_files = AsyncMock()
    svc.mark_last_assistant_failed = AsyncMock()
    return svc


def _make_request(upload_dao=None):
    request = MagicMock()
    request.app.state.upload_file_dao = upload_dao
    return request


async def _collect(gen):
    return [ev async for ev in gen]


_CITE_EVENTS = [
    'data: {"type": "policy_qa_citations", "citations": [{"title": "第一条", "url": "http://a"}]}\n\n',
    'data: {"type": "policy_qa_citations", "citations": [{"title": "第二条", "url": "http://b"}]}\n\n',
    'data: {"type": "summary", "content": "答案"}\n\n',
]


@pytest.mark.asyncio
async def test_generate_response_collects_citations_and_persists_pair(monkeypatch):
    """制度问答轮：多条 policy_qa_citations 事件累积合并随 assistant 消息落库，
    user/assistant 共享同值 message_pair_id，bind_message_id 三参调用。"""
    _patch_sensitive(monkeypatch)
    _patch_recommended_questions(monkeypatch)

    orch = _make_orch(_CITE_EVENTS)
    session_service = _make_session_service()
    upload_dao = MagicMock()
    upload_dao.bind_message_id = AsyncMock(return_value=1)

    await _collect(generate_response(
        orchestrator_service=orch,
        messages=[{"role": "user", "content": "报销制度是什么"}],
        session_id="sess-1",
        user_id="u1",
        session_service=session_service,
        langfuse_service=None,
        request=_make_request(upload_dao=upload_dao),
    ))

    session_service.append_messages.assert_awaited_once()
    sid, uid, persisted = session_service.append_messages.await_args[0]
    assert (sid, uid) == ("sess-1", "u1")

    user_msg = next(m for m in persisted if m["role"] == "user")
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    # user/assistant 共享同值 pair id（uuid4().hex）
    assert user_msg["message_pair_id"] == asst_msg["message_pair_id"]
    assert len(user_msg["message_pair_id"]) == 32
    # 两条 citations 事件累积合并为一条列表
    assert asst_msg["citations"] == [
        {"title": "第一条", "url": "http://a"},
        {"title": "第二条", "url": "http://b"},
    ]
    # user 消息不带 citations 键（仅 assistant 携带）
    assert "citations" not in user_msg

    # bind_message_id 三参：session_id + user_message_id + 本轮 pair id
    upload_dao.bind_message_id.assert_awaited_once_with(
        "sess-1", 101, user_msg["message_pair_id"]
    )


@pytest.mark.asyncio
async def test_generate_response_no_citations_persists_none(monkeypatch):
    """非制度问答轮（无 citations 事件）：assistant 消息 citations 为 None。"""
    _patch_sensitive(monkeypatch)
    _patch_recommended_questions(monkeypatch)

    orch = _make_orch(['data: {"type": "summary", "content": "答案"}\n\n'])
    session_service = _make_session_service()

    await _collect(generate_response(
        orchestrator_service=orch,
        messages=[{"role": "user", "content": "普通问题"}],
        session_id="sess-1",
        user_id="u1",
        session_service=session_service,
        langfuse_service=None,
        request=_make_request(upload_dao=None),
    ))

    session_service.append_messages.assert_awaited_once()
    persisted = session_service.append_messages.await_args[0][2]
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["citations"] is None
    # 配对 id 仍然生成且 user/assistant 共享
    user_msg = next(m for m in persisted if m["role"] == "user")
    assert user_msg["message_pair_id"] == asst_msg["message_pair_id"]


@pytest.mark.asyncio
async def test_generate_response_abort_persists_pair_and_citations(monkeypatch):
    """用户中断（cancel_event 置位）：CancelledError 进入 finally，
    落库仍携带 message_pair_id 与已捕获 citations，assistant success=False 兜底。"""
    _patch_sensitive(monkeypatch)
    _patch_recommended_questions(monkeypatch)

    cancel_event = asyncio.Event()

    async def fake_run(*args, **kwargs):
        yield 'data: {"type": "policy_qa_citations", "citations": [{"title": "中断前引用", "url": "http://c"}]}\n\n'
        yield 'data: {"type": "react_final", "conclusion": "中断前的结论"}\n\n'
        cancel_event.set()
        # 该事件不应被消费（下一轮循环检查到取消即 raise）
        yield 'data: {"type": "summary", "content": "不会被消费"}\n\n'

    orch = _make_orch(None, run_body=fake_run)
    session_service = _make_session_service()
    upload_dao = MagicMock()
    upload_dao.bind_message_id = AsyncMock(return_value=1)

    events = await _collect(generate_response(
        orchestrator_service=orch,
        messages=[{"role": "user", "content": "问题"}],
        session_id="sess-1",
        user_id="u1",
        session_service=session_service,
        langfuse_service=None,
        request=_make_request(upload_dao=upload_dao),
        cancel_event=cancel_event,
    ))

    # finally 路径落库：react_final 结论兜底为 assistant 内容
    session_service.append_messages.assert_awaited_once()
    persisted = session_service.append_messages.await_args[0][2]
    user_msg = next(m for m in persisted if m["role"] == "user")
    asst_msg = next(m for m in persisted if m["role"] == "assistant")
    assert asst_msg["content"] == "中断前的结论"
    # pair id 与已捕获 citations 均随中断轮落库
    assert user_msg["message_pair_id"] == asst_msg["message_pair_id"]
    assert len(asst_msg["message_pair_id"]) == 32
    assert asst_msg["citations"] == [{"title": "中断前引用", "url": "http://c"}]
    # 中断补 UPDATE：最新 assistant 消息 success 置 0
    session_service.mark_last_assistant_failed.assert_awaited_once_with("sess-1", "u1")
    # finally 路径不传 upload_file_dao → 不回填
    upload_dao.bind_message_id.assert_not_awaited()
    # 前端收到 user_abort 事件
    parsed = [json.loads(e[6:].strip()) for e in events if e.startswith("data: ")]
    assert "user_abort" in [p["type"] for p in parsed]
    # 取消后 summary 事件未被消费
    assert "summary" not in [p["type"] for p in parsed]
