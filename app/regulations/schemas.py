"""制度问答（regulations）请求/响应与数据大盘 Pydantic schema。

自 qa_app 移植：会话类 schema（ChatRequest/ChatInputs/SSE 事件/BlockingResponse）
不移植，SSE 事件 payload 由服务层直接用 dict 构造，不在本模块定义。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RetrieverResource(BaseModel):
    """单条引用资源（对齐 Dify RetrieverResource）。

    注意：position / dataset_id / document_id / document_name 等字段在写入路径
    （kb_client）恒有值，但历史数据回读可能缺。DB 存的 retriever_resources 是
    持久化快照，schema 须宽容缺失字段（带默认值），否则读旧会话会 500。
    """
    position: int = 0
    dataset_id: str = ""
    dataset_name: str = ""
    document_id: str = ""
    document_name: str = ""
    segment_id: str = ""
    score: float = 0.0
    content: str = ""
    page_start: int | None = None
    page_end: int | None = None
    element_ids: list[str] = []
    chunk_type: str | None = None


# ── 通用问答（无会话、不落库） ──────────────────────────────

class GeneralQARequest(BaseModel):
    """通用问答请求。"""
    question: str = Field(..., description="用户问题文本", min_length=1)
    knowledge_base_ids: list[str] = Field(
        ..., description="检索目标知识库 ID 列表（多库联合召回）", min_length=1,
    )
    app_serial_number: str | None = Field(
        default=None, description="调用方全链路流水号，不传则后端生成",
    )
    enable_query_understanding: bool = Field(
        default=False, description="是否启用 query 理解与改写；False 时直接用原 query 检索",
    )
    response_mode: Literal["blocking", "streaming"] = Field(
        default="blocking", description="响应模式：blocking 返回 JSON，streaming 返回 SSE",
    )


class GeneralQAResponse(BaseModel):
    """通用问答阻塞响应（不落库）。"""
    answer: str
    citations: list[RetrieverResource] = []
    app_serial_number: str = ""
    model: str = ""
    created_at: int = 0


# ── 数据大盘 ────────────────────────────────────────────────

class DashboardOverview(BaseModel):
    """大盘核心指标。"""
    published_knowledge: int = Field(..., description="已发布知识数量，单位：篇")
    active_search_users: int = Field(..., description="活跃检索用户 MAU，单位：人")
    valid_search_count: int = Field(..., description="有效检索量，单位：次")
    empty_answer_rate: float = Field(..., description="空应答率，单位：%")
    search_p95_latency: float = Field(..., description="检索 P95 耗时，单位：秒")
    active_kb_count: int = Field(..., description="活跃知识库数量")
    total_kb_count: int = Field(..., description="知识库总数量")


class DepartmentDistributionItem(BaseModel):
    """部门知识分布条目。"""
    department: str = Field(..., description="部门名称")
    knowledge_count: int = Field(..., description="该部门知识数量，单位：篇")
    percentage: float = Field(..., description="占已发布知识总数百分比，单位：%")


class SearchTrendItem(BaseModel):
    """近 30 天检索量与空应答数日趋势条目。"""
    date: str = Field(..., description="日期，格式 YYYY-MM-DD")
    search_count: int = Field(..., description="当日有效检索量")
    empty_answer_count: int = Field(..., description="当日空应答数")


class DepartmentUsageRankingItem(BaseModel):
    """部门使用与引用排行条目。"""
    department: str = Field(..., description="部门名称")
    search_count: int = Field(..., description="该部门用户产生的有效检索量")
    citation_count: int = Field(..., description="该部门知识被引用的次数")


class DepartmentUsageRanking(BaseModel):
    """部门使用与引用排行（汇总 + 排行列表）。"""
    total_departments: int = Field(..., description="部门总数")
    active_departments: int = Field(..., description="活跃部门数（近 30 天有检索）")
    zero_search_departments_30d: int = Field(..., description="近 30 天 0 检索的部门数")
    ranking: list[DepartmentUsageRankingItem] = Field(..., description="按 search_count 降序的部门排行")


class TopCitedKnowledgeItem(BaseModel):
    """高频引用知识条目。"""
    knowledge_name: str = Field(..., description="知识名称")
    citation_count: int = Field(..., description="引用次数（同一回答中重复引用只算一次）")
    unique_user_count: int = Field(..., description="引用该知识的独立用户数")
    department: str = Field(..., description="知识责任部门")
    last_updated: str = Field(..., description="最后更新时间，格式 YYYY-MM-DD")


class KnowledgeGapItem(BaseModel):
    """近期知识缺口条目（用户空检索问题已按意图分类）。"""
    question_type: str = Field(..., description="空检索问题的意图分类")
    empty_answer_count: int = Field(..., description="空应答次数")
    target_kb: str = Field(..., description="建议补充的目标知识库")


class DashboardData(BaseModel):
    """大盘页面聚合数据。"""
    overview: DashboardOverview
    search_trend: list[SearchTrendItem]
    department_distribution: list[DepartmentDistributionItem]
    department_usage_ranking: DepartmentUsageRanking
    top_cited_knowledge: list[TopCitedKnowledgeItem]
    knowledge_gaps: list[KnowledgeGapItem]


class DashboardResponse(BaseModel):
    """统一响应壳（对齐数据大盘契约）。"""
    code: int = 200
    message: str = "success"
    data: DashboardData


class ResolveKnowledgeGapRequest(BaseModel):
    """知识缺口 resolved 请求：按 gap_ids 或 kb_id 批量闭合（至少提供一个）。"""
    gap_ids: list[str] | None = None
    kb_id: str | None = None


class ResolveKnowledgeGapData(BaseModel):
    """知识缺口 resolved 结果。"""
    resolved_count: int = Field(..., description="本次闭合的缺口数")


class ResolveKnowledgeGapResponse(BaseModel):
    """统一响应壳。"""
    code: int = 200
    message: str = "success"
    data: ResolveKnowledgeGapData
