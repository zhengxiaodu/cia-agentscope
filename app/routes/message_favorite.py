"""收藏消息路由。

- POST /message_favorite：收藏（一批 message_pair_id 复制到收藏表，共享一个 favorite_id）
- DELETE /message_favorite/{favorite_id}：取消收藏（删除该 favorite_id 整组记录）
- GET /message_favorites：收藏详情（该用户全部收藏，按 favorite_id 分组）
"""
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request

from app.dependencies import current_user
from app.routes.message_share import _clean_pair_ids, error_response, success_response

router = APIRouter()


@router.post("/message_favorite")
async def create_message_favorite(
    request: Request,
    user: dict = Depends(current_user),
):
    """收藏消息：输入 message_pair_ids 列表（一个或多个），复制对应消息。"""
    favorite_dao = getattr(request.app.state, "message_favorite_dao", None)
    if favorite_dao is None:
        return error_response(500, "收藏服务未初始化")

    body = await request.json()
    pair_ids = _clean_pair_ids(body.get("message_pair_ids"))
    if not pair_ids:
        return error_response(400, "message_pair_ids 不能为空")

    favorite_id, copied = await favorite_dao.create_favorite(
        user.get("user_id"), pair_ids
    )
    if copied == 0:
        return error_response(404, "未找到对应消息")
    return success_response({"favorite_id": favorite_id, "count": copied})


@router.delete("/message_favorite/{favorite_id}")
async def delete_message_favorite(
    favorite_id: str,
    request: Request,
    user: dict = Depends(current_user),
):
    """取消收藏：删除该 favorite_id 对应的全部收藏记录。"""
    favorite_dao = getattr(request.app.state, "message_favorite_dao", None)
    if favorite_dao is None:
        return error_response(500, "收藏服务未初始化")

    deleted = await favorite_dao.delete_favorite(
        user.get("user_id"), favorite_id
    )
    if deleted == 0:
        return error_response(404, "收藏不存在")
    return success_response({"deleted": deleted})


@router.get("/message_favorites")
async def list_message_favorites(
    request: Request,
    user: dict = Depends(current_user),
):
    """收藏消息详情：返回该用户全部收藏，按 favorite_id 分组（收藏时间序）。"""
    favorite_dao = getattr(request.app.state, "message_favorite_dao", None)
    if favorite_dao is None:
        return error_response(500, "收藏服务未初始化")

    rows = await favorite_dao.list_favorites(user.get("user_id"))

    # 按 favorite_id 分组（dict 插入序 = 收藏先后；组内已按复制顺序排列）
    groups: Dict[str, List[dict]] = {}
    for row in rows:
        groups.setdefault(row["favorite_id"], []).append(row)

    favorites = [
        {"favorite_id": fid, "messages": messages}
        for fid, messages in groups.items()
    ]
    return success_response({"favorites": favorites})
