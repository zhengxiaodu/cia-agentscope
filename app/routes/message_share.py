"""消息分享路由。

- POST /message_share：创建分享（需登录，校验会话属主），返回 16 位 shared_id
- GET /message_share/{shared_id}：查看分享（无需登录，凭 shared_id 即可），
  复用 get_session_detail（传分享者 user_id 通过属主校验），
  messages/files/upload_files 三列表均按被分享的 message_pair_id 过滤
"""
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request

from app.dependencies import current_user

router = APIRouter()


def success_response(data: Any) -> Dict[str, Any]:
    return {"code": 200, "msg": "success", "data": data}


def error_response(code: int, msg: str) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": {}}


def _clean_pair_ids(raw) -> List[str]:
    """strip、去空、去重保序；清洗后为空返回空列表。"""
    if not isinstance(raw, list):
        return []
    seen = set()
    result = []
    for item in raw:
        if not isinstance(item, str):
            continue
        pid = item.strip()
        if pid and pid not in seen:
            seen.add(pid)
            result.append(pid)
    return result


@router.post("/message_share")
async def create_message_share(
    request: Request,
    user: dict = Depends(current_user),
):
    """创建消息分享：输入 session_id 与要分享的 message_pair_ids 列表。"""
    share_dao = getattr(request.app.state, "message_share_dao", None)
    session_dao = getattr(request.app.state, "session_dao", None)
    if share_dao is None or session_dao is None:
        return error_response(500, "分享服务未初始化")

    body = await request.json()
    session_id = str(body.get("session_id", "")).strip()
    pair_ids = _clean_pair_ids(body.get("message_pair_ids"))

    if not session_id:
        return error_response(400, "session_id 不能为空")
    if not pair_ids:
        return error_response(400, "message_pair_ids 不能为空")

    # 属主校验：只能分享自己的会话
    meta = await session_dao.get_session_meta(session_id)
    if meta is None:
        return error_response(404, "会话不存在")
    if meta.get("user_id") != user.get("user_id"):
        return error_response(403, "会话不属于当前用户")

    shared_id = await share_dao.create_share(
        session_id, user.get("user_id"), pair_ids
    )
    return success_response({"shared_id": shared_id})


@router.get("/message_share/{shared_id}")
async def get_message_share(shared_id: str, request: Request):
    """查看分享内容：无需登录，凭 shared_id 返回过滤后的会话消息片段。"""
    share_dao = getattr(request.app.state, "message_share_dao", None)
    session_service = getattr(request.app.state, "session_service", None)
    if share_dao is None or session_service is None:
        return error_response(500, "分享服务未初始化")

    share = await share_dao.get_share(shared_id)
    if share is None:
        return error_response(404, "分享不存在")

    # 用分享者的 user_id 调会话详情（通过 get_session_detail 的属主校验）
    detail = await session_service.get_session_detail(
        share["session_id"], share["user_id"]
    )
    if detail is None:
        return error_response(404, "会话不存在或已删除")

    pair_ids = set(share["message_pair_ids"])
    data = detail.model_dump(mode="json")
    data["shared_id"] = shared_id
    data["messages"] = [
        m for m in data["messages"]
        if m.get("message_pair_id") in pair_ids
    ]
    data["files"] = [
        f for f in data["files"]
        if f.get("message_pair_id") in pair_ids
    ]
    data["upload_files"] = [
        f for f in data["upload_files"]
        if f.get("message_pair_id") in pair_ids
    ]
    return success_response(data)
