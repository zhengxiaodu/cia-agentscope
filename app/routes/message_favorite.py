"""收藏消息路由。

- POST /message_favorite：收藏（一批 message_pair_id 复制到收藏表，共享一个 favorite_id）
- DELETE /message_favorite/{favorite_id}：取消收藏（删除该 favorite_id 整组记录）
- GET /message_favorites/list：收藏列表（轻量摘要：favorite_id / title /
  message_count / first_message_time，不含消息内容与文件）
- GET /message_favorites/{favorite_id}：收藏详情（该收藏完整消息 + files
  系统产出文件与 upload_files 用户上传文件，字段格式与历史会话详情接口对齐）
"""
import logging
from typing import Dict, List

from fastapi import APIRouter, Depends, Query, Request

from app.dependencies import current_user
from app.routes.message_share import (
    _clean_pair_ids,
    _first_user_message_title,
    _truncate_title,
    error_response,
    success_response,
)

logger = logging.getLogger(__name__)

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

    # 收藏标题：取组内首条用户消息截断 50 字符（查询失败返回空串，不阻断创建）
    raw = await favorite_dao.get_first_user_message(user.get("user_id"), pair_ids)
    title = _truncate_title(raw)

    favorite_id, copied = await favorite_dao.create_favorite(
        user.get("user_id"), pair_ids, title
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


@router.put("/message_favorite/name")
async def rename_message_favorite(
    request: Request,
    user: dict = Depends(current_user),
):
    """重命名收藏：输入 favorite_id 与新名称，修改该组收藏的 title。"""
    favorite_dao = getattr(request.app.state, "message_favorite_dao", None)
    if favorite_dao is None:
        return error_response(500, "收藏服务未初始化")

    body = await request.json()
    favorite_id = str(body.get("favorite_id", "")).strip()
    name = str(body.get("name", "")).strip()

    if not favorite_id:
        return error_response(400, "favorite_id 不能为空")
    if not name:
        return error_response(400, "收藏名称不能为空")
    if len(name) > 255:
        return error_response(400, "收藏名称不能超过255个字符")

    updated = await favorite_dao.rename_favorite(
        user.get("user_id"), favorite_id, name
    )
    if updated == 0:
        return error_response(404, "收藏不存在")
    return success_response({"favorite_id": favorite_id, "name": name})


@router.get("/message_favorites/list")
async def list_message_favorites(
    request: Request,
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，默认 20，最大 100"),
    user: dict = Depends(current_user),
):
    """收藏列表：分页返回该用户收藏的轻量摘要（收藏时间序）。

    每条仅含 favorite_id / title / message_count / first_message_time，
    不含消息内容与文件；详情按 favorite_id 走详情接口。
    title 为空的存量旧数据从组内首条用户消息兜底。
    """
    favorite_dao = getattr(request.app.state, "message_favorite_dao", None)
    if favorite_dao is None:
        return error_response(500, "收藏服务未初始化")

    user_id = user.get("user_id")
    total, summaries = await favorite_dao.list_favorite_summaries(
        user_id, page=page, page_size=page_size
    )

    favorites = []
    for s in summaries:
        title = s["title"]
        if not title:  # 存量旧数据兜底：组内首条用户消息截断
            raw = await favorite_dao.get_favorite_first_user_message(
                user_id, s["favorite_id"]
            )
            title = _truncate_title(raw)
        favorites.append({
            "favorite_id": s["favorite_id"],
            "title": title,
            "message_count": s["message_count"],
            "first_message_time": s["first_message_time"],
        })

    total_pages = (total + page_size - 1) // page_size
    return success_response({
        "favorites": favorites,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_more": page < total_pages,
    })


@router.get("/message_favorites/{favorite_id}")
async def get_message_favorite_detail(
    favorite_id: str,
    request: Request,
    user: dict = Depends(current_user),
):
    """收藏详情：按 favorite_id 返回该收藏的完整消息与关联文件。

    每条消息按 (session_id, message_pair_id) 匹配 files（系统产出文件）
    与 upload_files（用户上传文件）；跨会话收藏时各消息只匹配
    所属会话的文件。favorite_id 不存在或不属于当前用户返回 404。
    """
    favorite_dao = getattr(request.app.state, "message_favorite_dao", None)
    if favorite_dao is None:
        return error_response(500, "收藏服务未初始化")

    messages = await favorite_dao.get_favorite_messages(
        user.get("user_id"), favorite_id
    )
    if not messages:
        return error_response(404, "收藏不存在")

    # 按消息所属会话加载文件，再按 message_pair_id 过滤到组内
    keys = _collect_session_pair_keys(messages)
    files: List[dict] = []
    upload_files: List[dict] = []
    for sid, pair_ids in keys.items():
        loaded = await _load_session_files_pair(request, sid)
        files.extend(
            f for f in loaded["files"]
            if f.get("message_pair_id") in pair_ids
        )
        upload_files.extend(
            f for f in loaded["uploads"]
            if f.get("message_pair_id") in pair_ids
        )

    return success_response({
        "favorite_id": favorite_id,
        # 组内各行共享同一 title（创建时写入）；空则从组内消息兜底（存量旧数据）
        "title": messages[0].get("title") or _first_user_message_title(messages),
        "messages": messages,
        "files": files,
        "upload_files": upload_files,
    })


def _collect_session_pair_keys(messages: List[dict]) -> Dict[str, set]:
    """从分组消息收集 {session_id: set(message_pair_id)}。

    message_pair_id 为 None 的消息（理论上不存在，防御）不参与文件匹配。
    """
    keys: Dict[str, set] = {}
    for msg in messages:
        pair_id = msg.get("message_pair_id")
        if not pair_id:
            continue
        keys.setdefault(msg.get("session_id", ""), set()).add(pair_id)
    return keys


async def _load_session_files_pair(request: Request, session_id: str) -> dict:
    """查询单个会话的产出文件与上传文件，返回 {"files": [...], "uploads": [...]}。

    容错对齐 get_session_detail：upload_file_dao 缺失或查询异常时
    uploads 置 []；session_files 查询异常不吞（与详情接口一致）。
    """
    session_dao = getattr(request.app.state, "session_dao", None)
    upload_file_dao = getattr(request.app.state, "upload_file_dao", None)

    files = []
    if session_dao is not None:
        files = await session_dao.load_session_files(session_id)

    uploads = []
    if upload_file_dao is not None:
        try:
            uploads = await upload_file_dao.list_files_by_session(session_id)
        except Exception:
            logger.warning(
                "[message_favorite] 加载会话上传文件失败: %s",
                session_id,
                exc_info=True,
            )
    return {"files": files, "uploads": uploads}
