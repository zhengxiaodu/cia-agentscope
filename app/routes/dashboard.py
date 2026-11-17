"""数据大盘路由 — GET /dashboard/kb-overview、POST /dashboard/knowledge-gaps/resolve。

安全说明（越权修复）：
- 两个接口均强制 JWT 登录；登录态过期（Redis 无权限缓存）→ 401。
- 无制度问答权限（whitelist 无可映射知识库）→ 403。
- resolve 按 kb_id 操作时校验目标库在用户权限范围内，越权 → 403。
- gap_ids 路径暂无记录级归属校验，待管理员角色机制落地后收紧。
"""

import logging
from typing import List

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
from app.services.auth_service import get_user_permissions
from tools.policy_qa_tools import resolve_kb_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard")


async def _require_regulations_kb_ids(request: Request, user: dict) -> List[str]:
    """校验登录态并返回用户可访问的制度问答 kb_ids；未登录/过期/无权限抛 401/403。"""
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis 未就绪")
    perms_data = await get_user_permissions(redis_client, str(user.get("user_id")))
    if not perms_data:
        raise HTTPException(status_code=401, detail="用户登录态已过期，请重新登录")
    permissions = perms_data.get("permissions") or {}
    kb_ids = resolve_kb_ids(permissions)
    if not kb_ids:
        raise HTTPException(status_code=403, detail="无制度问答权限")
    return kb_ids


@router.get("/kb-overview", response_model=DashboardResponse)
async def kb_overview(
    request: Request,
    user: dict = Depends(current_user),
) -> DashboardResponse:
    """知识库数据大盘聚合接口：一次返回当前页面所需全部指标。

    - 需要登录且有制度问答权限。
    - REGULATIONS_DASHBOARD_USE_REAL_DATA=false 返回 Mock；true 查真实
      Langfuse/KB/缺口表（失败返回空数据，不回退 Mock）。
    """
    await _require_regulations_kb_ids(request, user)

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
    """标记知识缺口为 resolved（按 gap_ids 或 kb_id 批量），并失效大盘缓存。

    - 需要登录且有制度问答权限；kb_id 越权 → 403。
    """
    allowed_kb_ids = await _require_regulations_kb_ids(request, user)

    if not body.gap_ids and not body.kb_id:
        raise HTTPException(status_code=400, detail="需提供 gap_ids 或 kb_id 之一")

    if body.kb_id and body.kb_id not in allowed_kb_ids:
        raise HTTPException(status_code=403, detail="无权操作该知识库")

    gap_service = getattr(request.app.state, "regulations_gap_service", None)
    if gap_service is None:
        raise HTTPException(status_code=503, detail="知识缺口服务未就绪")

    count = await gap_service.resolve_gaps(
        gap_ids=body.gap_ids, kb_id=body.kb_id, allowed_kb_ids=allowed_kb_ids,
    )
    invalidate_dashboard_cache()
    return ResolveKnowledgeGapResponse(data=ResolveKnowledgeGapData(resolved_count=count))
