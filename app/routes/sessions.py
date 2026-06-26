from fastapi import APIRouter, Depends, Request
from typing import Any, Dict

from app.dependencies import current_user

router = APIRouter()


def success_response(data: Any) -> Dict[str, Any]:
    return {"code": 200, "msg": "success", "data": data}


def error_response(code: int, msg: str) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": {}}


def _get_session_service(request: Request):
    return request.app.state.session_service


@router.get("/sessions")
async def list_sessions(
    request: Request,
    user: dict = Depends(current_user),
):
    service = _get_session_service(request)
    top_session_list, session_list = await service.list_user_sessions(
        user.get("user_id"), limit=15
    )
    return success_response({
        "top_sessions": [s.model_dump(mode="json") for s in top_session_list],
        "sessions": [s.model_dump(mode="json") for s in session_list],
    })


@router.put("/sessions/{session_id}/pin")
async def pin_session(
    session_id: str,
    request: Request,
    user: dict = Depends(current_user),
):
    service = _get_session_service(request)
    body = await request.json()
    pinned = body.get("pinned", True)
    if pinned:
        await service.pin_session(user.get("user_id"), session_id)
    else:
        await service.unpin_session(user.get("user_id"), session_id)
    return success_response({"pinned": pinned})


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    request: Request,
    user: dict = Depends(current_user),
):
    service = _get_session_service(request)
    ok = await service.delete_session(user.get("user_id"), session_id)
    if not ok:
        return error_response(404, "会话不存在")
    return success_response({"deleted": True})


@router.get("/sessions/{session_id}")
async def get_session_detail(
    session_id: str,
    request: Request,
    user: dict = Depends(current_user),
):
    service = _get_session_service(request)
    try:
        detail = await service.get_session_detail(session_id, user.get("user_id"))
    except PermissionError:
        return error_response(403, "会话不属于当前用户")

    if detail is None:
        return error_response(404, "会话不存在")

    return success_response(detail.model_dump(mode="json"))