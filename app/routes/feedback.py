from fastapi import APIRouter, Depends, Request, HTTPException

from app.dependencies import current_user
from app.models.feedback import FeedbackRequest, FeedbackResponse

router = APIRouter()


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    body: FeedbackRequest,
    request: Request,
    user: dict = Depends(current_user),
):
    langfuse_service = request.app.state.langfuse_service
    if not langfuse_service or not langfuse_service.enabled:
        raise HTTPException(
            status_code=503,
            detail=FeedbackResponse(
                code=503,
                msg="Langfuse 服务未启用",
            ).model_dump(),
        )

    ok = langfuse_service.create_score(
        trace_id=body.trace_id,
        value=body.liked,
        comment=body.comment,
    )
    if not ok:
        raise HTTPException(
            status_code=502,
            detail=FeedbackResponse(
                code=502,
                msg="Langfuse 评分提交失败",
            ).model_dump(),
        )

    return FeedbackResponse(code=200, msg="success")