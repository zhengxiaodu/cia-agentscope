import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.dependencies import current_user
from app.models.chat import ChatRequest
from app.services.chat_service import generate_response
from app.dao.user_dao import fire_notify_mng_active

router = APIRouter()


class StopRequest(BaseModel):
    session_id: str


@router.post("/chat")
async def chat(request: Request, body: ChatRequest, user: dict = Depends(current_user)):
    # 异步上报 mng 用户活跃（fire-and-forget，失败不影响对话）
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        jwt_token = authorization.split(" ", 1)[1].strip()
        if jwt_token:
            fire_notify_mng_active(jwt_token)

    session_service = request.app.state.session_service
    user_id = user.get("user_id")

    # 获取或创建 session_id
    session_id = await session_service.get_or_create_session(body.session_id, user_id)

    # 创建取消标志并注册到 app.state.chat_tasks（供 /chat/stop 触发中断）
    cancel_event = asyncio.Event()
    request.app.state.chat_tasks[session_id] = cancel_event

    async def stream():
        try:
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
                search_enabled=body.search_enabled,
                skills=body.skills,
                cancel_event=cancel_event,
            ):
                yield event
        finally:
            # 流结束（正常/异常/中断）后清理注册表
            request.app.state.chat_tasks.pop(session_id, None)

    try:
        return StreamingResponse(stream(), media_type="text/event-stream")
    except Exception as e:
        request.app.state.chat_tasks.pop(session_id, None)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/stop")
async def stop_chat(
    request: Request,
    body: StopRequest,
    user: dict = Depends(current_user),
):
    """停止正在进行的会话。

    触发对应 session_id 的 cancel_event，generate_response 检测到后
    主动 raise CancelledError 进入 finally：落库（success=False）+
    flush trace + yield user_abort 事件。本接口立即返回，不等待中断完成。
    """
    session_id = body.session_id
    chat_tasks = getattr(request.app.state, "chat_tasks", {})
    cancel_event = chat_tasks.get(session_id)
    if cancel_event is None:
        return {"ok": False, "msg": "会话不在运行中或已结束"}
    cancel_event.set()
    return {"ok": True, "msg": "已发送停止信号"}
