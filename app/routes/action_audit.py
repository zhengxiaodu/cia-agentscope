import logging

from fastapi import APIRouter, Depends, Request, HTTPException

from app.dependencies import current_user
from app.models.action_audit import (
    ActionAuditRequest,
    ActionAuditResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/action-audit", response_model=ActionAuditResponse)
async def record_action_audit(
    body: ActionAuditRequest,
    request: Request,
    user: dict = Depends(current_user),
):
    userId = user.get("user_id")
    if not userId:
        raise HTTPException(
            status_code=401,
            detail=ActionAuditResponse(
                code=401, msg="token 中缺少 user_id"
            ).model_dump(),
        )

    audit_service = getattr(request.app.state, "action_audit_service", None)
    if audit_service is None:
        raise HTTPException(
            status_code=500,
            detail=ActionAuditResponse(
                code=500, msg="审计服务未就绪"
            ).model_dump(),
        )

    try:
        await audit_service.record_action(
            userId=userId,
            action=body.action,
            query=body.content.query,
            confirm=body.content.confirm,
        )
    except Exception:
        logger.exception(
            f"[action_audit] 记录审计日志失败 user={userId} action={body.action}"
        )
        raise HTTPException(
            status_code=500,
            detail=ActionAuditResponse(
                code=500, msg="记录审计日志失败"
            ).model_dump(),
        )

    return ActionAuditResponse(code=200, msg="success")
