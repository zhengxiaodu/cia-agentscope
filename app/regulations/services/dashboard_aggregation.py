"""数据大盘 A/A⁺/B 类指标客户端聚合 — 纯函数，无 IO，便于单测。

输入为 Langfuse v1 行级拉取的原始 dict（trace / observation），输出为
可直接装配进 DashboardData 的中间 dict。对旧埋点格式（蛇形字段）做兼容读取。
"""
from __future__ import annotations

import datetime as dt


# ── 字段兼容读取 ──────────────────────────────────────────

def _metadata(trace: dict) -> dict:
    return trace.get("metadata") or {}


def _output(trace: dict) -> dict:
    return trace.get("output") or {}


def _user_id(trace: dict) -> str:
    return trace.get("userId") or ""


def _kb_id(trace: dict) -> str:
    md = _metadata(trace)
    return md.get("kbId") or md.get("kb_id") or ""


def _user_dept(trace: dict) -> str:
    return _metadata(trace).get("userDepartment") or ""


def _is_empty(trace: dict) -> bool:
    md = _metadata(trace)
    out = _output(trace)
    return (md.get("resultType") or md.get("result_type") or out.get("result_type")) == "empty"


def is_refused(trace: dict) -> bool:
    """领域拒答 trace（未实际检索）：大盘聚合前统一过滤，不计入有效检索量。"""
    md = _metadata(trace)
    out = _output(trace)
    return (md.get("resultType") or md.get("result_type") or out.get("result_type")) == "refused"


def _cited_docs(trace: dict) -> list[str]:
    md = _metadata(trace)
    out = _output(trace)
    docs = md.get("citedDocs") or md.get("cited_docs") or out.get("citedDocs") or out.get("cited_docs") or []
    return [d for d in docs if d]


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    rank = (len(s) - 1) * p
    lo = int(rank)
    hi = lo + 1
    if hi >= len(s):
        return s[-1]
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def _trace_date(trace: dict) -> dt.date | None:
    ts = trace.get("timestamp")
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


# ── 指标聚合 ──────────────────────────────────────────────

def overview_real(
    traces: list[dict],
    latencies: list[float],
    current_kb_ids: set[str] | None = None,
) -> dict:
    """overview 里 5 个 A/A⁺/B 字段（C 字段由调用方填 Mock）。

    current_kb_ids: 当前仍存在的 KB 集合（stats.kb_map），排除已删除 KB 的幽灵引用。
    """
    valid = len(traces)
    users = {_user_id(t) for t in traces if _user_id(t)}
    kbs = {_kb_id(t) for t in traces if _kb_id(t)}
    if current_kb_ids is not None:
        kbs &= current_kb_ids
    empty = sum(1 for t in traces if _is_empty(t))
    rate = round(empty / valid * 100, 1) if valid else 0.0
    return {
        "valid_search_count": valid,
        "search_p95_latency": round(_percentile(latencies, 0.95), 2),
        "active_search_users": len(users),
        "active_kb_count": len(kbs),
        "empty_answer_rate": rate,
    }


def search_trend_real(traces: list[dict], lookback_days: int, *, now: dt.date | None = None) -> list[dict]:
    """近 lookback_days 天双序列（按 UTC 日分桶，升序）。"""
    today = now or dt.datetime.now().astimezone().date()
    days = [today - dt.timedelta(days=i) for i in range(lookback_days - 1, -1, -1)]
    search = {d: 0 for d in days}
    empty = {d: 0 for d in days}
    for t in traces:
        d = _trace_date(t)
        if d is not None and d in search:
            search[d] += 1
            if _is_empty(t):
                empty[d] += 1
    return [
        {"date": d.isoformat(), "search_count": search[d], "empty_answer_count": empty[d]}
        for d in days
    ]


def department_usage_real(
    traces: list[dict],
    doc_departments: dict[str, str] | None = None,
    current_departments: set[str] | None = None,
) -> dict:
    """部门使用排行：search_count（userDepartment）+ citation_count（citedDocs→责任部门 join）。

    doc_departments: doc_id → 责任部门名称（来自 documents/batch 的 C 类 join）。
    current_departments: 当前部门主表名称集合（stats.departments），排除已删除/改名部门的幽灵引用。
    ranking = 检索部门 ∪ 引用部门（任一有值就展示），按 (search_count, citation_count) 降序。
    """
    doc_departments = doc_departments or {}
    search_counts: dict[str, int] = {}
    citation_counts: dict[str, int] = {}
    for t in traces:
        dept = _user_dept(t)
        if dept:
            search_counts[dept] = search_counts.get(dept, 0) + 1
        for doc in _cited_docs(t):
            d = doc_departments.get(doc)
            if d:
                citation_counts[d] = citation_counts.get(d, 0) + 1
    if current_departments is not None:
        search_counts = {d: c for d, c in search_counts.items() if d in current_departments}
    all_depts = set(search_counts) | set(citation_counts)
    ranking = sorted(
        all_depts,
        key=lambda d: (search_counts.get(d, 0), citation_counts.get(d, 0)),
        reverse=True,
    )
    return {
        "active_departments": len(search_counts),
        "ranking": [
            {
                "department": d,
                "search_count": search_counts.get(d, 0),
                "citation_count": citation_counts.get(d, 0),
            }
            for d in ranking
        ],
    }


def collect_cited_doc_ids(traces: list[dict]) -> list[str]:
    """所有 trace 的 citedDocs 去重后的 docId 列表（供批量查文档元数据）。"""
    seen: set[str] = set()
    for t in traces:
        for doc in _cited_docs(t):
            if doc not in seen:
                seen.add(doc)
    return list(seen)


def top_cited_real(
    traces: list[dict],
    top_n: int = 4,
    valid_doc_ids: set[str] | None = None,
) -> list[dict]:
    """高频引用：按 (trace, doc) 去重得引用次数、(doc, user) 去重得独立用户。

    valid_doc_ids: 当前仍存在的文档集合（documents/batch 结果），排除已删除文档的幽灵引用。
    """
    cite: dict[str, int] = {}
    users: dict[str, set[str]] = {}
    for t in traces:
        uid = _user_id(t)
        for doc in _cited_docs(t):
            cite[doc] = cite.get(doc, 0) + 1
            if uid:
                users.setdefault(doc, set()).add(uid)
    if valid_doc_ids is not None:
        cite = {d: c for d, c in cite.items() if d in valid_doc_ids}
        users = {d: u for d, u in users.items() if d in valid_doc_ids}
    ranked = sorted(cite.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [
        {"doc_id": doc, "citation_count": cite[doc], "unique_user_count": len(users.get(doc, set()))}
        for doc, _ in ranked
    ]
