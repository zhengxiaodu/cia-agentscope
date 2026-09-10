"""知识缺口服务与 DAO 单测：record_gap 生命周期 / 哈希规范化 / summarize / DAO SQL 路径。

DAO 用假 pool/连接/cursor mock（不依赖真实 MySQL）；LLM 概括用 monkeypatch mock。
"""
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

import app.regulations.services.knowledge_gap_service as kgs_module
from app.regulations.dao.knowledge_gap_dao import KnowledgeGapDAO
from app.regulations.services.knowledge_gap_service import (
    KnowledgeGapService,
    _normalize,
    _question_hash,
)


# ---- 假连接池（DAO SQL 路径） ----

class _FakeCursor:
    def __init__(self, rows=None, first_row=None, rowcount=0, exc=None):
        self._rows = rows or []
        self._first_row = first_row
        self.rowcount = rowcount
        self._exc = exc
        self.executed = []  # [(sql, params)]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._exc:
            raise self._exc

    async def fetchone(self):
        return self._first_row

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.begun = 0
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def cursor(self, cursor_class=None):
        return self._cursor

    async def begin(self):
        self.begun += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _FakePool:
    def __init__(self, cursor):
        self._conn = _FakeConn(cursor)

    def acquire(self):
        class _Ctx:
            async def __aenter__(_self):
                return self._conn

            async def __aexit__(_self, *args):
                return False

        return _Ctx()


# ---- 哈希规范化 ----

def test_normalize_collapses_whitespace():
    assert _normalize("  差旅   报销\n怎么 办？ ") == "差旅 报销 怎么 办？"
    assert _normalize("") == ""


def test_question_hash_normalizes_before_hashing():
    """空白差异（多空格/换行/首尾）归一后哈希一致。"""
    h1 = _question_hash("kb1", "差旅 报销 怎么办？")
    h2 = _question_hash("kb1", "  差旅\n报销   怎么办？  ")
    assert h1 == h2
    assert h1 == hashlib.sha256("kb1\x00差旅 报销 怎么办？".encode()).hexdigest()


def test_question_hash_differs_by_kb():
    assert _question_hash("kb1", "q") != _question_hash("kb2", "q")


# ---- KnowledgeGapService.record_gap ----

def _make_service():
    dao = MagicMock()
    dao.upsert_open_gap = AsyncMock(return_value=True)
    dao.update_question_type = AsyncMock()
    svc = KnowledgeGapService(dao, model_cfg={"model_name": "test-model"})
    return svc, dao


@pytest.mark.asyncio
async def test_record_gap_new_triggers_summarize_and_update(monkeypatch):
    """新缺口（upsert 返回 True）→ summarize + update_question_type 一次。"""
    svc, dao = _make_service()
    summarize = AsyncMock(return_value="差旅报销")
    monkeypatch.setattr(svc, "summarize_question_type", summarize)

    await svc.record_gap("kb1", "差旅报销怎么规定的？")

    dao.upsert_open_gap.assert_awaited_once()
    args = dao.upsert_open_gap.await_args[0]
    assert args[0] == "kb1"
    assert args[1] == "差旅报销怎么规定的？"
    assert args[2] == _question_hash("kb1", "差旅报销怎么规定的？")

    summarize.assert_awaited_once_with("差旅报销怎么规定的？")
    dao.update_question_type.assert_awaited_once_with(
        "kb1", _question_hash("kb1", "差旅报销怎么规定的？"), "差旅报销",
    )


@pytest.mark.asyncio
async def test_record_gap_existing_only_increments(monkeypatch):
    """已存在缺口（upsert 返回 False）→ 只累加，不概括不回写。"""
    svc, dao = _make_service()
    dao.upsert_open_gap = AsyncMock(return_value=False)
    summarize = AsyncMock(return_value="主题")
    monkeypatch.setattr(svc, "summarize_question_type", summarize)

    await svc.record_gap("kb1", "同一问题")

    dao.upsert_open_gap.assert_awaited_once()
    summarize.assert_not_awaited()
    dao.update_question_type.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_gap_dedups_whitespace_variants(monkeypatch):
    """仅空白差异的两次提问 → 传给 DAO 的 question_hash 相同（同问去重）。"""
    svc, dao = _make_service()
    summarize = AsyncMock(return_value="主题")
    monkeypatch.setattr(svc, "summarize_question_type", summarize)

    await svc.record_gap("kb1", "  差旅 报销  怎么办？ ")
    await svc.record_gap("kb1", "差旅\n报销 怎么办？")

    hashes = [call.args[2] for call in dao.upsert_open_gap.await_args_list]
    assert hashes[0] == hashes[1]
    assert dao.update_question_type.await_count == 2  # 两次都返回 True（mock）


@pytest.mark.parametrize("kb_id,question", [("", "q"), ("kb1", ""), ("", "")])
@pytest.mark.asyncio
async def test_record_gap_empty_inputs_skipped(kb_id, question):
    """kb_id 或 question 为空 → 不触 DAO。"""
    svc, dao = _make_service()
    await svc.record_gap(kb_id, question)
    dao.upsert_open_gap.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_gap_dao_error_swallowed(monkeypatch):
    """DAO upsert 异常 → 后台任务吞掉（不打断主流程），不概括。"""
    svc, dao = _make_service()
    dao.upsert_open_gap = AsyncMock(side_effect=RuntimeError("db down"))
    summarize = AsyncMock(return_value="主题")
    monkeypatch.setattr(svc, "summarize_question_type", summarize)

    await svc.record_gap("kb1", "q")  # 不应抛出

    summarize.assert_not_awaited()
    dao.update_question_type.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_gap_summarize_empty_skips_update(monkeypatch):
    """概括失败返回空串 → 不回写 question_type（缺口以「未分类」展示）。"""
    svc, dao = _make_service()
    monkeypatch.setattr(svc, "summarize_question_type", AsyncMock(return_value=""))

    await svc.record_gap("kb1", "q")

    dao.update_question_type.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_gap_update_error_swallowed(monkeypatch):
    """update_question_type 异常 → 吞掉不上抛。"""
    svc, dao = _make_service()
    dao.update_question_type = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(svc, "summarize_question_type", AsyncMock(return_value="主题"))

    await svc.record_gap("kb1", "q")  # 不应抛出


# ---- summarize_question_type ----

def _patch_llm(monkeypatch, content=None, error=None):
    class _FakeLLM:
        async def ainvoke(self, prompt, **kwargs):
            if error:
                raise error
            return AIMessage(content=content)

    monkeypatch.setattr(kgs_module, "get_rewrite_llm", lambda cfg: _FakeLLM())


@pytest.mark.asyncio
async def test_summarize_returns_llm_text(monkeypatch):
    _patch_llm(monkeypatch, content="差旅报销标准")
    svc, _ = _make_service()
    assert await svc.summarize_question_type("差旅报销制度？") == "差旅报销标准"


@pytest.mark.asyncio
async def test_summarize_strips_quotes_and_truncates(monkeypatch):
    """剥离引号/空白并截断到 64 字符。"""
    _patch_llm(monkeypatch, content="“差旅报销”\n")
    svc, _ = _make_service()
    assert await svc.summarize_question_type("q") == "差旅报销"

    long = "主" * 80
    _patch_llm(monkeypatch, content=long)
    svc2, _ = _make_service()
    assert await svc2.summarize_question_type("q") == "主" * 64


@pytest.mark.asyncio
async def test_summarize_empty_on_llm_failure(monkeypatch):
    _patch_llm(monkeypatch, error=RuntimeError("llm down"))
    svc, _ = _make_service()
    assert await svc.summarize_question_type("q") == ""


@pytest.mark.asyncio
async def test_summarize_empty_on_blank_question():
    svc, _ = _make_service()
    assert await svc.summarize_question_type("   ") == ""


# ---- KnowledgeGapService 读 / 闭合 ----

@pytest.mark.asyncio
async def test_load_open_gaps_grouped_delegates_to_dao():
    svc, dao = _make_service()
    dao.list_open_gaps_grouped = AsyncMock(return_value=[{"kb_id": "kb1"}])
    assert await svc.load_open_gaps_grouped() == [{"kb_id": "kb1"}]


@pytest.mark.asyncio
async def test_resolve_gaps_delegates_to_dao():
    svc, dao = _make_service()
    dao.resolve_gaps = AsyncMock(return_value=3)
    n = await svc.resolve_gaps(gap_ids=["g1", "g2"], kb_id=None)
    assert n == 3
    dao.resolve_gaps.assert_awaited_once_with(gap_ids=["g1", "g2"], kb_id=None)


# ---- KnowledgeGapDAO SQL 路径（假 pool） ----

@pytest.mark.asyncio
async def test_dao_upsert_existing_row_updates_and_returns_false():
    """已有 open 记录 → UPDATE empty_count+1，返回 False。"""
    cur = _FakeCursor(first_row={"id": 7})
    dao = KnowledgeGapDAO(_FakePool(cur))

    is_new = await dao.upsert_open_gap("kb1", "q", "h1")

    assert is_new is False
    sqls = [s for s, _ in cur.executed]
    assert any("SELECT id FROM knowledge_gaps" in s for s in sqls)
    assert any("UPDATE knowledge_gaps" in s and "empty_count = empty_count + 1" in s for s in sqls)
    update_params = cur.executed[1][1]
    assert update_params == (7,)


@pytest.mark.asyncio
async def test_dao_upsert_new_row_inserts_and_returns_true():
    """无 open 记录 → INSERT 新缺口（uuid hex 作 gap_id），返回 True。"""
    cur = _FakeCursor(first_row=None)
    dao = KnowledgeGapDAO(_FakePool(cur))

    is_new = await dao.upsert_open_gap("kb1", "q", "h1")

    assert is_new is True
    insert_sql, insert_params = cur.executed[1]
    assert "INSERT INTO knowledge_gaps" in insert_sql
    assert insert_params[1:] == ("kb1", "q", "h1")  # gap_id 为 uuid4().hex
    assert len(insert_params[0]) == 32


@pytest.mark.asyncio
async def test_dao_upsert_rolls_back_on_error():
    """SQL 异常 → rollback 且异常上抛。"""
    cur = _FakeCursor(exc=RuntimeError("syntax error"))
    pool = _FakePool(cur)
    dao = KnowledgeGapDAO(pool)

    with pytest.raises(RuntimeError):
        await dao.upsert_open_gap("kb1", "q", "h1")

    assert pool._conn.rollbacks == 1
    assert pool._conn.commits == 0


@pytest.mark.asyncio
async def test_dao_update_question_type_executes_sql():
    cur = _FakeCursor()
    dao = KnowledgeGapDAO(_FakePool(cur))

    await dao.update_question_type("kb1", "h1", "差旅报销")

    sql, params = cur.executed[0]
    assert "UPDATE knowledge_gaps" in sql
    assert "question_type = %s" in sql
    assert params == ("差旅报销", "kb1", "h1")


@pytest.mark.asyncio
async def test_dao_list_open_gaps_grouped_maps_rows():
    cur = _FakeCursor(rows=[
        {"kb_id": "kb1", "question_type": "差旅报销", "cnt": 3},
        {"kb_id": "kb2", "question_type": "未分类", "cnt": 1},
    ])
    dao = KnowledgeGapDAO(_FakePool(cur))

    rows = await dao.list_open_gaps_grouped()

    assert rows == [
        {"kb_id": "kb1", "question_type": "差旅报销", "empty_answer_count": 3},
        {"kb_id": "kb2", "question_type": "未分类", "empty_answer_count": 1},
    ]
    sql, _ = cur.executed[0]
    assert "status = 'open'" in sql
    assert "GROUP BY kb_id, question_type" in sql


@pytest.mark.asyncio
async def test_dao_resolve_gaps_by_gap_ids():
    """按 gap_ids 批量闭合：IN 占位符与参数一一对应，返回 rowcount。"""
    cur = _FakeCursor(rowcount=2)
    dao = KnowledgeGapDAO(_FakePool(cur))

    n = await dao.resolve_gaps(gap_ids=["g1", "g2"])

    assert n == 2
    sql, params = cur.executed[0]
    assert "status = 'resolved'" in sql
    assert "gap_id IN (%s, %s)" in sql
    assert params == ("g1", "g2")


@pytest.mark.asyncio
async def test_dao_resolve_gaps_by_kb_id():
    cur = _FakeCursor(rowcount=5)
    dao = KnowledgeGapDAO(_FakePool(cur))

    n = await dao.resolve_gaps(kb_id="kb1")

    assert n == 5
    sql, params = cur.executed[0]
    assert "kb_id = %s" in sql
    assert params == ("kb1",)


@pytest.mark.asyncio
async def test_dao_resolve_gaps_noop_without_args():
    """gap_ids 与 kb_id 均未提供 → 不执行 SQL，返回 0。"""
    cur = _FakeCursor()
    dao = KnowledgeGapDAO(_FakePool(cur))

    n = await dao.resolve_gaps()

    assert n == 0
    assert cur.executed == []
