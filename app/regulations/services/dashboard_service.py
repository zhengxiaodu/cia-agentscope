"""知识库数据大盘数据来源与编排（自 qa_app 移植）。

真实数据接入时，仅替换本模块内部的数据来源（改为查询 KB / Langfuse 等），
不改变 DashboardData 返回结构。

移植适配：loguru→标准 logging；get_settings()→app.regulations.config 常量；
knowledge_gaps 查库由 AsyncSession 改为 KnowledgeGapDAO。
"""

from __future__ import annotations

import datetime as _dt
import logging
import time

from app.regulations.config import (
    LANGFUSE_HOST,
    REGULATIONS_DASHBOARD_LOOKBACK_DAYS,
    REGULATIONS_DASHBOARD_USE_REAL_DATA,
    REGULATIONS_KB_API_BASE_URL,
    REGULATIONS_KB_INTERNAL_TOKEN,
    REGULATIONS_LANGFUSE_PUBLIC_KEY,
    REGULATIONS_LANGFUSE_SECRET_KEY,
)
from app.regulations.dao.knowledge_gap_dao import KnowledgeGapDAO
from app.regulations.schemas import (
    DashboardData,
    DashboardOverview,
    DepartmentDistributionItem,
    DepartmentUsageRanking,
    DepartmentUsageRankingItem,
    KnowledgeGapItem,
    SearchTrendItem,
    TopCitedKnowledgeItem,
)
from app.regulations.services.dashboard_aggregation import (
    collect_cited_doc_ids,
    department_usage_real,
    is_refused,
    overview_real,
    search_trend_real,
    top_cited_real,
)
from app.regulations.services.kb_dashboard_client import KBDashboardClient, KBDashboardQueryError
from app.regulations.services.langfuse_query import LangfuseQueryClient, LangfuseQueryError

logger = logging.getLogger(__name__)

# ── Mock 数据 ────────────────────────────────────────────────
# 每个字段标注数据源分级（详见 docs/dashboard-data-source-mapping.md）：
#   A  · Langfuse 原生聚合（无需埋点，直接查指标接口）
#   A⁺ · Langfuse 行级去重（行级拉取后客户端去重）
#   B  · Langfuse 业务埋点（业务判定打标后走 Langfuse）
#   C  · 知识库/外部数据（Langfuse 无法提供，需外部数据源）

# 部门维度：knowledge_count 之和 = published_knowledge；
#           search_count 之和 = valid_search_count。
# 字段：(department, knowledge_count[C·外部], search_count[B·埋点], citation_count[B·埋点])
_DEPARTMENTS: list[tuple[str, int, int, int]] = [
    ("产品研发部", 3186, 48620, 12840),
    ("运营管理部", 2574, 37410, 9630),
    ("人力资源部", 2180, 30180, 7280),
    ("财务管理部", 1900, 26430, 8410),
    ("法务合规部", 1643, 21850, 6130),
    ("信息技术部", 1363, 21930, 5570),
]

# overview 指标
_PUBLISHED_KNOWLEDGE = 12846    # C · 外部：知识库内容库「文档表 status=已发布」计数
_ACTIVE_SEARCH_USERS = 2486     # A⁺· 行级：trace userId 去重
_VALID_SEARCH_COUNT = 186420    # A · 原生：observations count（kb_search + root）
_EMPTY_ANSWER_RATE = 3.1        # B · 埋点：score empty_answer(true) ÷ 有效检索量
_SEARCH_P95_LATENCY = 0.86      # A · 原生：retrieval span latency p95
_ACTIVE_KB_COUNT = 18           # A⁺· 行级：metadata.kbId 去重
_TOTAL_KB_COUNT = 21            # C · 外部：知识库配置表

# 部门使用与引用排行汇总：排行列表仅展示 top 活跃部门，汇总覆盖全量部门
_TOTAL_DEPARTMENTS = 12         # C · 外部：部门主表
_ACTIVE_DEPARTMENTS = 10        # B · 埋点：metadata.userDepartment 去重
_ZERO_SEARCH_DEPARTMENTS_30D = 2  # 推算：部门主表 − 活跃部门

# 高频引用知识（citation_count 降序；department 须落在部门分布内）
_TOP_CITED_KNOWLEDGE: list[tuple[str, int, int, str, str]] = [
    # (knowledge_name[C·外部], citation_count[B·埋点], unique_user_count[B·埋点],
    #  department[C·外部], last_updated[C·外部])
    ("差旅报销制度（2024版）", 1842, 768, "财务管理部", "2026-07-28"),
    ("产品功能 FAQ 汇总", 1521, 634, "产品研发部", "2026-08-10"),
    ("请假与考勤管理规范", 1308, 552, "人力资源部", "2026-06-15"),
    ("项目交付模板（标准版）", 986, 421, "运营管理部", "2026-05-22"),
]

# 近期知识缺口（empty_answer_count 降序）；question_type 为对空检索问题的「制度查询意图」分类
_KNOWLEDGE_GAPS: list[tuple[str, int, str]] = [
    # (question_type[B·埋点+LLM聚类], empty_answer_count[B·埋点], target_kb[B·埋点·kbId])
    ("制度查询类", 128, "法务知识库"),
    ("流程办理类", 96, "采购知识库"),
    ("标准额度类", 74, "财务知识库"),
    ("责任权限类", 61, "产品知识库"),
]

# 近 30 天趋势：分三段（每 10 天），检索量阶段上升、空应答率阶段下降。
# 段内仍保留「工作日高 / 周末低」的日波动；整体检索量再归一化精确到 valid_search_count。
_TREND_END_DATE = _dt.date(2026, 8, 23)
_TREND_DAYS = 30
_TREND_STAGE_SIZE = 10
# (工作日基数, 周末基数, 空应答率%)
# 检索量[A·原生] 与空应答数[B·埋点]，均按 timeDimension=day 聚合
_TREND_STAGES: list[tuple[int, int, float]] = [
    (5200, 2860, 4.0),
    (6400, 3520, 3.1),
    (7600, 4180, 2.3),
]


def _iter_trend_days():
    """按升序产出近 _TREND_DAYS 个自然日。"""
    start = _TREND_END_DATE - _dt.timedelta(days=_TREND_DAYS - 1)
    for i in range(_TREND_DAYS):
        yield start + _dt.timedelta(days=i)


def _build_search_trend() -> list[SearchTrendItem]:
    """生成近 30 天趋势：检索量三段上升、空应答率三段下降，检索量之和精确等于 valid_search_count。"""
    days = list(_iter_trend_days())
    raw_counts: list[int] = []
    raw_rates: list[float] = []
    for i, day in enumerate(days):
        weekday_base, weekend_base, rate = _TREND_STAGES[i // _TREND_STAGE_SIZE]
        raw_counts.append(weekday_base if day.weekday() < 5 else weekend_base)
        raw_rates.append(rate)

    scale = _VALID_SEARCH_COUNT / sum(raw_counts)
    counts = [round(c * scale) for c in raw_counts]
    counts[-1] += _VALID_SEARCH_COUNT - sum(counts)  # 修正取整误差，保证精确和

    items: list[SearchTrendItem] = []
    for day, count, rate in zip(days, counts, raw_rates):
        items.append(
            SearchTrendItem(
                date=day.isoformat(),
                search_count=count,
                empty_answer_count=round(count * rate / 100),
            )
        )
    return items


def _mock_department_distribution() -> list[DepartmentDistributionItem]:
    """部门知识分布（C 类 Mock，真实/模拟路径共用）。"""
    distribution = [
        DepartmentDistributionItem(
            department=department,
            knowledge_count=knowledge_count,
            percentage=round(knowledge_count / _PUBLISHED_KNOWLEDGE * 100, 1),
        )
        for department, knowledge_count, _, _ in _DEPARTMENTS
    ]
    distribution.sort(key=lambda item: item.knowledge_count, reverse=True)
    return distribution


def _build_mock_data() -> DashboardData:
    """组装大盘页面所需的全部 mock 数据（DASHBOARD_USE_REAL_DATA=false 时使用）。"""
    distribution = _mock_department_distribution()

    ranking_items = [
        DepartmentUsageRankingItem(
            department=department, search_count=search_count, citation_count=citation_count,
        )
        for department, _, search_count, citation_count in _DEPARTMENTS
    ]
    ranking_items.sort(key=lambda item: item.search_count, reverse=True)

    top_cited = [
        TopCitedKnowledgeItem(
            knowledge_name=name,
            citation_count=citation_count,
            unique_user_count=unique_user_count,
            department=department,
            last_updated=last_updated,
        )
        for name, citation_count, unique_user_count, department, last_updated in _TOP_CITED_KNOWLEDGE
    ]

    gaps = [
        KnowledgeGapItem(
            question_type=question_type,
            empty_answer_count=empty_answer_count,
            target_kb=target_kb,
        )
        for question_type, empty_answer_count, target_kb in _KNOWLEDGE_GAPS
    ]

    return DashboardData(
        overview=DashboardOverview(
            published_knowledge=_PUBLISHED_KNOWLEDGE,
            active_search_users=_ACTIVE_SEARCH_USERS,
            valid_search_count=_VALID_SEARCH_COUNT,
            empty_answer_rate=_EMPTY_ANSWER_RATE,
            search_p95_latency=_SEARCH_P95_LATENCY,
            active_kb_count=_ACTIVE_KB_COUNT,
            total_kb_count=_TOTAL_KB_COUNT,
        ),
        search_trend=_build_search_trend(),
        department_distribution=distribution,
        department_usage_ranking=DepartmentUsageRanking(
            total_departments=_TOTAL_DEPARTMENTS,
            active_departments=_ACTIVE_DEPARTMENTS,
            zero_search_departments_30d=_ZERO_SEARCH_DEPARTMENTS_30D,
            ranking=ranking_items,
        ),
        top_cited_knowledge=top_cited,
        knowledge_gaps=gaps,
    )


# ── 真实数据编排 ──────────────────────────────────────────
# 真实模式缓存：单条（整页），TTL 秒。失败不缓存（下次重试）。
_real_cache: dict = {"ts": 0.0, "data": None}
_REAL_CACHE_TTL_SECONDS = 300


def invalidate_dashboard_cache() -> None:
    """失效真实数据缓存（缺口 resolved 后立即生效，无需等 TTL）。"""
    _real_cache["data"] = None


async def build_dashboard_data(gap_dao: KnowledgeGapDAO) -> DashboardData:
    """组装大盘数据：REGULATIONS_DASHBOARD_USE_REAL_DATA=false 走 Mock，true 查真实 Langfuse/KB/缺口表。"""
    if not REGULATIONS_DASHBOARD_USE_REAL_DATA:
        return _build_mock_data()

    now = time.time()
    if _real_cache["data"] is not None and now - _real_cache["ts"] < _REAL_CACHE_TTL_SECONDS:
        return _real_cache["data"]

    try:
        data = await _build_real_data(gap_dao)
    except (LangfuseQueryError, KBDashboardQueryError) as exc:
        logger.error(f"dashboard 真实数据查询失败，返回空数据: {exc}")
        return _empty_dashboard_data()

    _real_cache["ts"] = now
    _real_cache["data"] = data
    return data


async def _build_real_data(gap_dao: KnowledgeGapDAO) -> DashboardData:
    lf = LangfuseQueryClient(
        host=LANGFUSE_HOST,
        public_key=REGULATIONS_LANGFUSE_PUBLIC_KEY,
        secret_key=REGULATIONS_LANGFUSE_SECRET_KEY,
    )
    kb = KBDashboardClient(
        base_url=REGULATIONS_KB_API_BASE_URL,
        token=REGULATIONS_KB_INTERNAL_TOKEN,
    )
    now = _dt.datetime.now(_dt.timezone.utc)
    frm = now - _dt.timedelta(days=REGULATIONS_DASHBOARD_LOOKBACK_DAYS)
    traces = await lf.list_traces(frm, now)
    # 领域拒答（未实际检索）不计入任何「检索」类指标，聚合前统一过滤
    traces = [t for t in traces if not is_refused(t)]
    retrieval_obs = await lf.list_retrieval_latencies(frm, now)
    # P95 口径与 valid_search_count 对齐：只统计 kb_search 父 trace 的 retrieval span，
    # 排除埋点对齐前非 kb_search 旧 trace 的 retrieval span。
    kb_trace_ids = {t.get("id") for t in traces}
    latencies = [r["latency"] for r in retrieval_obs if r["trace_id"] in kb_trace_ids]
    stats = await kb.get_stats()

    doc_meta = await kb.get_documents_batch(collect_cited_doc_ids(traces))

    # 当前主数据（用于排除已删除 KB/部门/文档的幽灵引用）
    current_kb_ids = {k["kb_id"] for k in stats.get("kb_map", [])}
    current_departments = set(stats.get("departments", []))
    doc_meta_ids = {d["doc_id"] for d in doc_meta}

    overview = overview_real(traces, latencies, current_kb_ids)
    trend = search_trend_real(traces, REGULATIONS_DASHBOARD_LOOKBACK_DAYS)
    doc_departments = {d["doc_id"]: d["department"] for d in doc_meta if d.get("department")}
    usage = department_usage_real(traces, doc_departments, current_departments)
    cited = top_cited_real(traces, top_n=4, valid_doc_ids=doc_meta_ids)
    gaps = (await gap_dao.list_open_gaps_grouped())[:4]  # 只读 open 缺口（resolved 不再展示）
    return _assemble_dashboard_data(overview, trend, usage, cited, gaps, stats, doc_meta)


def _assemble_dashboard_data(overview, trend, usage, cited, gaps, stats, doc_meta) -> DashboardData:
    """把真实 A/A⁺/B 字段与 C 类主数据（stats / documents batch）合并为 DashboardData。"""
    ranking_items = [
        DepartmentUsageRankingItem(
            department=item["department"],
            search_count=item["search_count"],
            citation_count=item["citation_count"],
        )
        for item in usage["ranking"]
    ]
    doc_meta_by_id = {d["doc_id"]: d for d in doc_meta}
    top_cited_items = [
        TopCitedKnowledgeItem(
            knowledge_name=doc_meta_by_id.get(item["doc_id"], {}).get("title", ""),
            citation_count=item["citation_count"],
            unique_user_count=item["unique_user_count"],
            department=doc_meta_by_id.get(item["doc_id"], {}).get("department") or "",
            last_updated=doc_meta_by_id.get(item["doc_id"], {}).get("updated_at", ""),
        )
        for item in cited
    ]
    kb_map = {k["kb_id"]: k["kb_name"] for k in stats.get("kb_map", [])}
    gap_items = [
        KnowledgeGapItem(
            question_type=item.get("question_type", ""),
            empty_answer_count=item["empty_answer_count"],
            target_kb=kb_map.get(item["kb_id"], item["kb_id"]),
        )
        for item in gaps
    ]
    published = stats.get("published_knowledge", 0)
    distribution = [
        DepartmentDistributionItem(
            department=item.get("department") or "未分配",
            knowledge_count=item.get("knowledge_count", 0),
            percentage=round(item.get("knowledge_count", 0) / published * 100, 1) if published else 0.0,
        )
        for item in stats.get("department_distribution", [])
    ]
    distribution.sort(key=lambda item: item.knowledge_count, reverse=True)
    total_departments = stats.get("total_departments", 0)
    return DashboardData(
        overview=DashboardOverview(
            published_knowledge=published,
            active_search_users=overview["active_search_users"],
            valid_search_count=overview["valid_search_count"],
            empty_answer_rate=overview["empty_answer_rate"],
            search_p95_latency=overview["search_p95_latency"],
            active_kb_count=overview["active_kb_count"],
            total_kb_count=stats.get("total_kb_count", 0),
        ),
        search_trend=[SearchTrendItem(**t) for t in trend],
        department_distribution=distribution,
        department_usage_ranking=DepartmentUsageRanking(
            total_departments=total_departments,
            active_departments=usage["active_departments"],
            zero_search_departments_30d=max(0, total_departments - usage["active_departments"]),
            ranking=ranking_items,
        ),
        top_cited_knowledge=top_cited_items,
        knowledge_gaps=gap_items,
    )


def _empty_dashboard_data() -> DashboardData:
    """真实模式查询失败时的空数据（全 0 / 空列表），不回退 Mock。"""
    return DashboardData(
        overview=DashboardOverview(
            published_knowledge=0, active_search_users=0, valid_search_count=0,
            empty_answer_rate=0.0, search_p95_latency=0.0,
            active_kb_count=0, total_kb_count=0,
        ),
        search_trend=[],
        department_distribution=[],
        department_usage_ranking=DepartmentUsageRanking(
            total_departments=0, active_departments=0,
            zero_search_departments_30d=0, ranking=[],
        ),
        top_cited_knowledge=[],
        knowledge_gaps=[],
    )
