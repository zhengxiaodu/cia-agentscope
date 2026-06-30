import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.dependencies import current_user
from app.models.chat import ChatRequest
from app.services.chat_service import generate_response

router = APIRouter()


@router.post("/chat")
async def chat(request: Request, body: ChatRequest, user: dict = Depends(current_user)):
    session_service = request.app.state.session_service
    user_id = user.get("user_id")

    # 获取或创建 session_id
    session_id = await session_service.get_or_create_session(body.session_id, user_id)

    async def stream():
        # 先发送 SESSION_READY 事件告知前端 session_id
        session_event = {
            "type": "session_ready",
            "session_id": session_id,
        }
        yield f"data: {json.dumps(session_event, ensure_ascii=False)}\n\n"

        # 再发送聊天流式事件（多智能体编排 / 单智能体直接问答）
        async for event in generate_response(
            orchestrator_service=request.app.state.orchestrator_service,
            messages=body.messages,
            session_id=session_id,
            user_id=user_id,
            session_service=session_service,
            langfuse_service=request.app.state.langfuse_service,
            agent_id=body.agent_id,
            request=request,
        ):
            yield event

    try:
        return StreamingResponse(stream(), media_type="text/event-stream")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
