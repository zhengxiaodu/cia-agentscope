import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.dependencies import current_user
from app.models.feedback import FeedbackRequest, FeedbackResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    body: FeedbackRequest,
    request: Request,
    user: dict = Depends(current_user),
):
    """点赞点踩反馈：落库为主 + Langfuse 双写（尽力而为）。

    落库：messages.feedback（1=赞 0=踩），按 (session_id, message_pair_id)
    定位本轮 assistant 消息行，行级 user_id 条件防越权；刷新会话详情即可回显。
    Langfuse：评分写入失败不影响接口结果（仅告警），未启用时跳过。
    """
    session_dao = request.app.state.session_dao

    # 1) 落库（主路径）
    feedback_value = 1 if body.liked else 0
    # 评价内容：去除首尾空白，截断到 VARCHAR(1024) 上限内
    comment = (body.comment or "").strip() or None
    if comment:
        comment = comment[:1024]
    updated = await session_dao.update_message_feedback(
        session_id=body.session_id,
        user_id=str(user.get("user_id", "")),
        message_pair_id=body.message_pair_id,
        feedback=feedback_value,
        comment=comment,
    )
    if updated == 0:
        raise HTTPException(
            status_code=404,
            detail=FeedbackResponse(
                code=404,
                msg="消息不存在或无权操作",
            ).model_dump(),
        )

    # 2) Langfuse 双写（尽力而为，失败不阻断）
    langfuse_service = request.app.state.langfuse_service
    if langfuse_service and langfuse_service.enabled:
        try:
            ok = langfuse_service.create_score(
                trace_id=body.trace_id,
                value=body.liked,
                comment=comment,
            )
            if not ok:
                logger.warning(
                    "[feedback] Langfuse 评分提交失败（不影响落库）trace=%s",
                    body.trace_id,
                )
        except Exception:
            logger.warning("[feedback] Langfuse 评分提交异常（不影响落库）", exc_info=True)

    return FeedbackResponse(code=200, msg="success")
