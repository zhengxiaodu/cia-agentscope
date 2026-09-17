"""制度问答路由 — POST /api/v1/policy/qa（blocking/streaming，无状态，不落库）。

响应保持原制度问答后端契约（answer/citations/app_serial_number/model/created_at
直接在顶层，SSE 事件同 /chat-messages），不套本项目 success_response 包装；
出错时以 HTTPException 返回（本项目无全局 AppError handler，路由内局部处理）。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.dependencies import current_user
from app.regulations.schemas import GeneralQARequest, GeneralQAResponse
from app.regulations.services.kb_client import KBClientError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/v1/policy/qa", response_model=None)
async def policy_qa(
    request: Request,
    body: GeneralQARequest,
    user: dict = Depends(current_user),
) -> GeneralQAResponse | StreamingResponse:
    """通用问答：传入问题 + 知识库列表，返回回答正文与引用文档。

    - response_mode=blocking（默认）：阻塞返回 JSON（GeneralQAResponse）。
    - response_mode=streaming：返回 SSE 流（对齐 /chat-messages 事件契约）。
    - 无状态：不建会话、不落库，出口打一条 [policy-qa] 结果日志（只记概要，
      不记正文；正文与引用写 Langfuse trace 供溯源）。
    - kb_ids 直接用传参值，不受 REGULATIONS_TOP_K_OVERRIDE 影响。
    - KBClientError → 502，其余异常 → 500。
    """
    svc = getattr(request.app.state, "policy_qa_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="制度问答服务未就绪")

    user_id = str(user.get("user_id"))
    user_department = user.get("department") or ""

    try:
        if body.response_mode == "streaming":
            return await svc.run_general_qa_stream(
                question=body.question,
                kb_ids=body.knowledge_base_ids,
                serial=body.app_serial_number,
                user_id=user_id,
                user_department=user_department,
                enable_query_understanding=body.enable_query_understanding,
            )
        return await svc.run_general_qa(
            question=body.question,
            kb_ids=body.knowledge_base_ids,
            serial=body.app_serial_number,
            user_id=user_id,
            user_department=user_department,
            enable_query_understanding=body.enable_query_understanding,
        )
    except KBClientError as e:
        logger.error(f"[policy-qa] 知识库检索失败 user={user_id}: {e}")
        raise HTTPException(status_code=502, detail=f"知识库检索失败: {e}") from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[policy-qa] 内部错误 user={user_id}")
        raise HTTPException(status_code=500, detail=f"内部服务错误: {e}") from e
