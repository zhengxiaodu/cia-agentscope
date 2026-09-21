"""收藏消息路由。

- POST /message_favorite：收藏（一批 message_pair_id 复制到收藏表，共享一个 favorite_id）
- DELETE /message_favorite/{favorite_id}：取消收藏（删除该 favorite_id 整组记录）
- GET /message_favorites：收藏详情（该用户全部收藏，按 favorite_id 分组，
  每个分组附 files 系统产出文件与 upload_files 用户上传文件，
  字段格式与历史会话详情接口对齐）
"""
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request

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
    user: dict = Depends(current_user),
):
    """收藏消息详情：返回该用户全部收藏，按 favorite_id 分组（收藏时间序）。

    每个分组附 files（系统产出文件）与 upload_files（用户上传文件），
    按 (session_id, message_pair_id) 与组内消息关联；跨会话收藏时
    各消息只匹配所属会话的文件。
    """
    favorite_dao = getattr(request.app.state, "message_favorite_dao", None)
    if favorite_dao is None:
        return error_response(500, "收藏服务未初始化")

    rows = await favorite_dao.list_favorites(user.get("user_id"))

    # 按 favorite_id 分组（dict 插入序 = 收藏先后；组内已按复制顺序排列）
    groups: Dict[str, List[dict]] = {}
    for row in rows:
        groups.setdefault(row["favorite_id"], []).append(row)

    # 每个分组涉及的 {session_id: set(message_pair_id)}
    group_pair_keys: Dict[str, Dict[str, set]] = {
        fid: _collect_session_pair_keys(messages)
        for fid, messages in groups.items()
    }

    # 全局收集唯一 session_id，每个 session 只查一次库，结果缓存复用
    session_files_cache: Dict[str, Dict[str, list]] = {}
    for sid in {sid for keys in group_pair_keys.values() for sid in keys}:
        session_files_cache[sid] = await _load_session_files_pair(
            request, sid
        )

    favorites = []
    for fid, messages in groups.items():
        keys = group_pair_keys[fid]
        files: List[dict] = []
        upload_files: List[dict] = []
        for sid, pair_ids in keys.items():
            cached = session_files_cache.get(sid, {"files": [], "uploads": []})
            files.extend(
                f for f in cached["files"]
                if f.get("message_pair_id") in pair_ids
            )
            upload_files.extend(
                f for f in cached["uploads"]
                if f.get("message_pair_id") in pair_ids
            )
        favorites.append({
            "favorite_id": fid,
            # 组内各行共享同一 title（创建时写入）；空则从组内消息兜底（存量旧数据）
            "title": (messages[0].get("title") if messages else "")
                     or _first_user_message_title(messages),
            "messages": messages,
            "files": files,
            "upload_files": upload_files,
        })
    return success_response({"favorites": favorites})


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
