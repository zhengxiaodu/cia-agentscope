"""数据大盘单测：Mock 模式结构 / 真实模式编排与缓存 / 聚合纯函数。

真实模式用 monkeypatch 替换 LangfuseQueryClient / KBDashboardClient /
gap_dao，不触外部服务。
"""
import datetime as dt
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.regulations.services.dashboard_service as dsvc
from app.regulations.schemas import DashboardData
from app.regulations.services.dashboard_aggregation import (
    collect_cited_doc_ids,
    department_usage_real,
    is_refused,
    overview_real,
    search_trend_real,
    top_cited_real,
)
from app.regulations.services.dashboard_service import (
    build_dashboard_data,
    invalidate_dashboard_cache,
)
from app.regulations.services.kb_dashboard_client import KBDashboardQueryError
from app.regulations.services.langfuse_query import LangfuseQueryError


# ---- Mock 模式 ----

@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch):
    """强制 Mock 模式 + 复位真实缓存，隔离环境变量与跨用例缓存。"""
    monkeypatch.setattr(dsvc, "REGULATIONS_DASHBOARD_USE_REAL_DATA", False)
    monkeypatch.setattr(dsvc, "_real_cache", {"ts": 0.0, "data": None})


@pytest.mark.asyncio
async def test_mock_mode_returns_full_dashboard_structure():
    """Mock 模式返回完整 DashboardData：六块指标齐全。"""
    data = await build_dashboard_data(MagicMock())

    assert isinstance(data, DashboardData)
    o = data.overview
    assert o.published_knowledge > 0
    assert o.active_search_users > 0
    assert o.valid_search_count > 0
    assert 0 <= o.empty_answer_rate < 100
    assert o.search_p95_latency > 0
    assert 0 < o.active_kb_count <= o.total_kb_count

    assert len(data.search_trend) > 0
    assert len(data.department_distribution) > 0
    assert data.department_usage_ranking.ranking
    assert data.top_cited_knowledge
    assert data.knowledge_gaps


@pytest.mark.asyncio
async def test_mock_trend_covers_30_days_and_sums_to_valid():
    """趋势 30 天、日期连续升序，检索量总和精确等于 valid_search_count。"""
    data = await build_dashboard_data(MagicMock())
    trend = data.search_trend

    assert len(trend) == 30
    dates = [t.date for t in trend]
    assert dates == sorted(dates)
    start = dt.date.fromisoformat(dates[0])
    assert dates == [(start + dt.timedelta(days=i)).isoformat() for i in range(30)]
    assert sum(t.search_count for t in trend) == data.overview.valid_search_count
    for t in trend:
        assert 0 <= t.empty_answer_count < t.search_count


@pytest.mark.asyncio
async def test_mock_department_sums_match_overview():
    """部门知识分布之和 = published_knowledge；部门检索量之和 = valid_search_count。"""
    data = await build_dashboard_data(MagicMock())

    assert sum(d.knowledge_count for d in data.department_distribution) == \
        data.overview.published_knowledge
    assert sum(d.search_count for d in data.department_usage_ranking.ranking) == \
        data.overview.valid_search_count


@pytest.mark.asyncio
async def test_mock_rankings_sorted_descending():
    data = await build_dashboard_data(MagicMock())

    usage = [d.search_count for d in data.department_usage_ranking.ranking]
    cited = [d.citation_count for d in data.top_cited_knowledge]
    gaps = [d.empty_answer_count for d in data.knowledge_gaps]
    assert usage == sorted(usage, reverse=True)
    assert cited == sorted(cited, reverse=True)
    assert gaps == sorted(gaps, reverse=True)


@pytest.mark.asyncio
async def test_mock_usage_ranking_summary_consistent():
    data = await build_dashboard_data(MagicMock())
    summary = data.department_usage_ranking
    assert summary.total_departments > 0
    assert summary.active_departments + summary.zero_search_departments_30d == \
        summary.total_departments


# ---- 真实模式：fake 客户端数据 ----

_TRACES = [
    {"id": "t1", "userId": "u1", "timestamp": "2026-08-20T10:00:00Z",
     "metadata": {"kbId": "kb1", "userDepartment": "财务管理部", "resultType": "normal",
                  "citedDocs": ["d1"]}},
    {"id": "t2", "userId": "u2", "timestamp": "2026-08-20T11:00:00Z",
     "metadata": {"kbId": "kb2", "userDepartment": "产品研发部", "resultType": "empty",
                  "citedDocs": ["d1"]}},
]

_LATENCIES = [{"trace_id": "t1", "latency": 1.0}, {"trace_id": "t2", "latency": 2.0}]

_STATS = {
    "total_kb_count": 2,
    "kb_map": [
        {"kb_id": "kb1", "kb_name": "默认知识库"},
        {"kb_id": "kb2", "kb_name": "财务知识库"},
    ],
    "published_knowledge": 10,
    "department_distribution": [
        {"department": "财务管理部", "knowledge_count": 7},
        {"department": None, "knowledge_count": 3},
    ],
    "total_departments": 6,
    "departments": ["产品研发部", "运营管理部", "财务管理部"],
}

_BATCH = [{"doc_id": "d1", "title": "差旅报销制度", "department": "财务管理部",
           "updated_at": "2026-07-28"}]


class _FakeQueryClient:
    instances = 0

    def __init__(self, host, public_key, secret_key, **kw):
        _FakeQueryClient.instances += 1

    async def list_traces(self, frm, to):
        return _TRACES

    async def list_retrieval_latencies(self, frm, to):
        return _LATENCIES


class _FakeStatsClient:
    def __init__(self, base_url, token, **kw):
        pass

    async def get_stats(self):
        return _STATS

    async def get_documents_batch(self, doc_ids):
        return [d for d in _BATCH if d["doc_id"] in doc_ids]


def _gap_dao():
    dao = MagicMock()
    dao.list_open_gaps_grouped = AsyncMock(return_value=[
        {"kb_id": "kb2", "question_type": "流程办理", "empty_answer_count": 1},
    ])
    return dao


@pytest.fixture
def _real_mode(monkeypatch):
    monkeypatch.setattr(dsvc, "REGULATIONS_DASHBOARD_USE_REAL_DATA", True)
    monkeypatch.setattr(dsvc, "LangfuseQueryClient", _FakeQueryClient)
    monkeypatch.setattr(dsvc, "KBDashboardClient", _FakeStatsClient)
    monkeypatch.setattr(dsvc, "_real_cache", {"ts": 0.0, "data": None})
    _FakeQueryClient.instances = 0


@pytest.mark.asyncio
async def test_real_mode_assembles_data(_real_mode):
    """真实模式：A/A⁺/B 聚合 + C 类主数据 join 装配 DashboardData。"""
    data = await build_dashboard_data(_gap_dao())

    o = data.overview
    assert o.valid_search_count == 2
    assert o.active_search_users == 2
    assert o.active_kb_count == 2
    assert o.empty_answer_rate == 50.0
    assert o.search_p95_latency == pytest.approx(1.95)
    assert o.published_knowledge == 10
    assert o.total_kb_count == 2

    assert sum(d.knowledge_count for d in data.department_distribution) == 10
    assert data.department_distribution[0].department == "财务管理部"

    ranking = data.department_usage_ranking
    assert ranking.total_departments == 6
    assert ranking.active_departments == 2
    assert ranking.zero_search_departments_30d == 4
    assert ranking.ranking[0].department == "财务管理部"
    assert ranking.ranking[0].citation_count == 2
    assert ranking.ranking[0].search_count == 1

    top = data.top_cited_knowledge[0]
    assert top.knowledge_name == "差旅报销制度"
    assert top.department == "财务管理部"
    assert top.last_updated == "2026-07-28"
    assert top.citation_count == 2
    assert top.unique_user_count == 2

    # 缺口 target_kb 由 kb_id 映射为 kb_name
    gap = data.knowledge_gaps[0]
    assert gap.question_type == "流程办理"
    assert gap.target_kb == "财务知识库"


@pytest.mark.asyncio
async def test_real_mode_caches_within_ttl(_real_mode):
    """300s TTL 内重复调用命中缓存：不重查 Langfuse，返回同一对象。"""
    first = await build_dashboard_data(_gap_dao())
    second = await build_dashboard_data(_gap_dao())

    assert _FakeQueryClient.instances == 1
    assert second is first


@pytest.mark.asyncio
async def test_real_mode_cache_expires_after_ttl(_real_mode):
    """缓存过期（>300s）后重新查询。"""
    first = await build_dashboard_data(_gap_dao())
    dsvc._real_cache["ts"] = time.time() - 301
    second = await build_dashboard_data(_gap_dao())

    assert _FakeQueryClient.instances == 2
    assert second is not first


@pytest.mark.asyncio
async def test_real_mode_invalidate_cache_forces_requery(_real_mode):
    """invalidate_dashboard_cache 后缓存失效，立即重查（缺口 resolved 即时生效）。"""
    first = await build_dashboard_data(_gap_dao())
    invalidate_dashboard_cache()
    second = await build_dashboard_data(_gap_dao())

    assert _FakeQueryClient.instances == 2
    assert dsvc._real_cache["data"] is second


@pytest.mark.asyncio
async def test_real_mode_langfuse_failure_returns_empty_not_mock(monkeypatch):
    """真实模式 Langfuse 查询失败 → 返回空数据，不回退 Mock。"""
    class _BoomClient:
        def __init__(self, host, public_key, secret_key, **kw):
            pass

        async def list_traces(self, frm, to):
            raise LangfuseQueryError("Langfuse 请求失败: connection refused")

        async def list_retrieval_latencies(self, frm, to):
            return []

    monkeypatch.setattr(dsvc, "REGULATIONS_DASHBOARD_USE_REAL_DATA", True)
    monkeypatch.setattr(dsvc, "LangfuseQueryClient", _BoomClient)
    monkeypatch.setattr(dsvc, "KBDashboardClient", _FakeStatsClient)
    monkeypatch.setattr(dsvc, "_real_cache", {"ts": 0.0, "data": None})

    data = await build_dashboard_data(_gap_dao())

    o = data.overview
    assert o.valid_search_count == 0
    assert o.published_knowledge == 0
    assert o.total_kb_count == 0
    assert data.search_trend == []
    assert data.department_distribution == []
    assert data.department_usage_ranking.ranking == []
    assert data.top_cited_knowledge == []
    assert data.knowledge_gaps == []
    # 不回退 Mock（Mock 模式 valid_search_count=186420 / published=12846）
    assert o.valid_search_count != dsvc._VALID_SEARCH_COUNT
    assert o.published_knowledge != dsvc._PUBLISHED_KNOWLEDGE


@pytest.mark.asyncio
async def test_real_mode_kb_stats_failure_returns_empty(monkeypatch):
    """真实模式 KB 大盘主数据查询失败 → 同样返回空数据。"""
    class _BoomStats:
        def __init__(self, base_url, token, **kw):
            pass

        async def get_stats(self):
            raise KBDashboardQueryError("知识库大盘接口请求失败")

    monkeypatch.setattr(dsvc, "REGULATIONS_DASHBOARD_USE_REAL_DATA", True)
    monkeypatch.setattr(dsvc, "LangfuseQueryClient", _FakeQueryClient)
    monkeypatch.setattr(dsvc, "KBDashboardClient", _BoomStats)
    monkeypatch.setattr(dsvc, "_real_cache", {"ts": 0.0, "data": None})

    data = await build_dashboard_data(_gap_dao())
    assert data.overview.valid_search_count == 0
    assert data.overview.total_kb_count == 0


@pytest.mark.asyncio
async def test_real_mode_filters_refused_traces(monkeypatch):
    """领域拒答 trace（resultType=refused）不计入有效检索量。"""
    refused = {"id": "t3", "userId": "u3", "timestamp": "2026-08-20T12:00:00Z",
               "metadata": {"kbId": "kb1", "resultType": "refused", "citedDocs": []}}

    class _RefusedClient(_FakeQueryClient):
        async def list_traces(self, frm, to):
            return _TRACES + [refused]

    monkeypatch.setattr(dsvc, "REGULATIONS_DASHBOARD_USE_REAL_DATA", True)
    monkeypatch.setattr(dsvc, "LangfuseQueryClient", _RefusedClient)
    monkeypatch.setattr(dsvc, "KBDashboardClient", _FakeStatsClient)
    monkeypatch.setattr(dsvc, "_real_cache", {"ts": 0.0, "data": None})

    data = await build_dashboard_data(_gap_dao())
    assert data.overview.valid_search_count == 2  # refused 被过滤


# ---- 聚合纯函数 ----

def _trace(uid="u1", kb="kb1", dept="研发", empty=False, docs=None,
           ts="2026-08-20T10:00:00Z", result_type=None):
    md = {
        "kbId": kb,
        "userDepartment": dept,
        "resultType": result_type or ("empty" if empty else "normal"),
        "citedDocs": docs or [],
    }
    return {"userId": uid, "timestamp": ts, "metadata": md}


def test_overview_real_counts_and_dedups():
    traces = [
        _trace(uid="u1"),
        _trace(uid="u1"),                 # 同一用户两次
        _trace(uid="u2", kb="kb2"),
        _trace(uid="", kb="kb1"),         # 无 userId 不计用户
        _trace(empty=True),
    ]
    r = overview_real(traces, [1.0, 2.0, 3.0, 4.0])
    assert r["valid_search_count"] == 5
    assert r["active_search_users"] == 2
    assert r["active_kb_count"] == 2
    assert r["empty_answer_rate"] == 20.0
    assert r["search_p95_latency"] == pytest.approx(3.85)


def test_overview_real_empty_inputs():
    r = overview_real([], [])
    assert r["valid_search_count"] == 0
    assert r["search_p95_latency"] == 0.0
    assert r["empty_answer_rate"] == 0.0


def test_overview_real_excludes_deleted_kb():
    r = overview_real([_trace(kb="kb1"), _trace(kb="kb2")], [], current_kb_ids={"kb1"})
    assert r["active_kb_count"] == 1


def test_is_refused_variants():
    assert is_refused({"metadata": {"resultType": "refused"}}) is True
    assert is_refused({"metadata": {"result_type": "refused"}}) is True
    assert is_refused({"output": {"result_type": "refused"}}) is True
    assert is_refused({"metadata": {"resultType": "empty"}}) is False
    assert is_refused({"metadata": {"resultType": "normal"}}) is False
    assert is_refused({}) is False


def test_top_cited_real_dedups_and_ranks():
    """引用次数按 (trace, doc) 计、独立用户按 (doc, user) 去重、按次数降序。"""
    traces = [
        _trace(uid="u1", docs=["d1", "d2"]),
        _trace(uid="u2", docs=["d1"]),
        _trace(uid="u1", docs=["d1", "d3"]),
    ]
    r = top_cited_real(traces, top_n=4)
    assert r[0] == {"doc_id": "d1", "citation_count": 3, "unique_user_count": 2}
    assert {x["doc_id"] for x in r} == {"d1", "d2", "d3"}


def test_top_cited_real_excludes_deleted_docs():
    r = top_cited_real([_trace(docs=["d1"]), _trace(docs=["d2"])],
                       top_n=4, valid_doc_ids={"d1"})
    assert r == [{"doc_id": "d1", "citation_count": 1, "unique_user_count": 1}]


def test_top_cited_real_top_n_limit():
    traces = [_trace(uid="u1", docs=["d1"]), _trace(uid="u2", docs=["d2"])]
    assert top_cited_real(traces, top_n=1) == [
        {"doc_id": "d1", "citation_count": 1, "unique_user_count": 1},
    ]


def test_department_usage_real_union_and_ranks():
    """检索部门 ∪ 引用部门（引用-only 部门 search_count=0 也展示）。"""
    traces = [_trace(dept="研发", docs=["d1"]), _trace(dept="")]
    r = department_usage_real(traces, {"d1": "财务"})
    assert r["active_departments"] == 1  # 有检索的只有研发
    assert r["ranking"] == [
        {"department": "研发", "search_count": 1, "citation_count": 0},
        {"department": "财务", "search_count": 0, "citation_count": 1},
    ]


def test_search_trend_real_buckets_by_day():
    traces = [
        _trace(ts="2026-08-24T10:00:00Z"),
        _trace(ts="2026-08-24T11:00:00Z", empty=True),
        _trace(ts="2026-08-23T23:00:00Z"),
    ]
    trend = search_trend_real(traces, 3, now=dt.date(2026, 8, 24))
    assert [t["date"] for t in trend] == ["2026-08-22", "2026-08-23", "2026-08-24"]
    assert trend[2]["search_count"] == 2
    assert trend[2]["empty_answer_count"] == 1


def test_collect_cited_doc_ids_dedups():
    traces = [_trace(docs=["d1", "d2"]), _trace(docs=["d2", "d3", ""])]
    assert set(collect_cited_doc_ids(traces)) == {"d1", "d2", "d3"}


def test_assemble_dashboard_data_maps_doc_meta(monkeypatch):
    """_assemble_dashboard_data：doc 元数据缺失时兜底空串而非 KeyError。"""
    overview = {"active_search_users": 1, "valid_search_count": 1, "empty_answer_rate": 0.0,
                "search_p95_latency": 0.5, "active_kb_count": 1}
    trend = [{"date": "2026-08-20", "search_count": 1, "empty_answer_count": 0}]
    usage = {"active_departments": 1, "ranking": [
        {"department": "研发", "search_count": 1, "citation_count": 1},
    ]}
    cited = [{"doc_id": "dX", "citation_count": 1, "unique_user_count": 1}]  # 元数据缺失
    gaps = [{"kb_id": "kb1", "question_type": "t", "empty_answer_count": 1}]
    stats = {"kb_map": [{"kb_id": "kb1", "kb_name": "库A"}], "published_knowledge": 5,
             "department_distribution": [], "total_departments": 2, "total_kb_count": 1}

    data = dsvc._assemble_dashboard_data(overview, trend, usage, cited, gaps, stats, [])

    assert data.top_cited_knowledge[0].knowledge_name == ""
    assert data.knowledge_gaps[0].target_kb == "库A"
    assert data.overview.published_knowledge == 5
    assert data.department_usage_ranking.zero_search_departments_30d == 1
