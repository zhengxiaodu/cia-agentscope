"""数据大盘路由 — GET /dashboard/kb-overview、POST /dashboard/knowledge-gaps/resolve。"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.dependencies import current_user
from app.regulations.schemas import (
    DashboardResponse,
    ResolveKnowledgeGapData,
    ResolveKnowledgeGapRequest,
    ResolveKnowledgeGapResponse,
)
from app.regulations.services.dashboard_service import (
    build_dashboard_data,
    invalidate_dashboard_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard")


@router.get("/kb-overview", response_model=DashboardResponse)
async def kb_overview(
    request: Request,
    user: dict = Depends(current_user),
) -> DashboardResponse:
    """知识库数据大盘聚合接口：一次返回当前页面所需全部指标。

    - REGULATIONS_DASHBOARD_USE_REAL_DATA=false 返回 Mock；true 查真实
      Langfuse/KB/缺口表（失败返回空数据，不回退 Mock）。
    """
    gap_dao = getattr(request.app.state, "regulations_gap_dao", None)
    if gap_dao is None:
        raise HTTPException(status_code=503, detail="知识缺口服务未就绪")
    return DashboardResponse(data=await build_dashboard_data(gap_dao))


@router.post("/knowledge-gaps/resolve", response_model=ResolveKnowledgeGapResponse)
async def resolve_knowledge_gaps(
    request: Request,
    body: ResolveKnowledgeGapRequest,
    user: dict = Depends(current_user),
) -> ResolveKnowledgeGapResponse:
    """标记知识缺口为 resolved（按 gap_ids 或 kb_id 批量），并失效大盘缓存。"""
    if not body.gap_ids and not body.kb_id:
        raise HTTPException(status_code=400, detail="需提供 gap_ids 或 kb_id 之一")

    gap_service = getattr(request.app.state, "regulations_gap_service", None)
    if gap_service is None:
        raise HTTPException(status_code=503, detail="知识缺口服务未就绪")

    count = await gap_service.resolve_gaps(gap_ids=body.gap_ids, kb_id=body.kb_id)
    invalidate_dashboard_cache()
    return ResolveKnowledgeGapResponse(data=ResolveKnowledgeGapData(resolved_count=count))
