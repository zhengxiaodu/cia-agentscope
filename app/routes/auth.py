"""登录路由：调用 mng 校验 → 存 Redis 权限 → 生成 JWT 返回前端。"""
from fastapi import APIRouter, HTTPException, Request
from typing import Any, Dict

from app.dao.user_dao import verify_login, register as register_user
from app.models.auth import LoginRequest, RegisterRequest
from app.services.auth_service import (
    create_access_token,
    save_user_permissions,
)
from app.config import JWT_EXPIRE_HOURS

router = APIRouter()


def success_response(data: Any) -> Dict[str, Any]:
    return {"code": 200, "msg": "success", "data": data}


def error_response(code: int, msg: str) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": {}}


async def _build_auth_success(result: dict, request: Request) -> dict:
    """登录/注册成功后的统一处理：存 Redis 权限 → 生成 JWT → 构造前端响应。

    login 与 register 共用此函数，保证返回前端的字段结构逐字段一致。
    """
    user_info = result.get("user_info", {}) or {}
    user_id = user_info.get("id")
    access_token = result.get("access_token", "")
    permissions = result.get("permissions", {}) or {}

    # 将 mng 返回的 access_token 和 permissions 按 user_id 存入 Redis，
    # 方便后续 /chat 接口查询用户权限（用于获取外部意图 + 权限过滤）
    if user_id:
        redis_client = getattr(request.app.state, "redis_client", None)
        if redis_client is not None:
            try:
                await save_user_permissions(redis_client, user_id, access_token, permissions)
            except Exception:
                # Redis 写入失败不阻断主流程
                import logging
                logging.getLogger(__name__).exception(
                    f"[auth] 保存用户 {user_id} 权限到 Redis 失败"
                )

    # 自己生成 JWT 返回前端（payload 只放基础信息，权限走 Redis 查询）
    token_payload = {
        "user_id": user_id,
        "user_name": user_info.get("user_name", ""),
        "department": user_info.get("department", ""),
        "role": user_info.get("role", ""),
    }
    token = create_access_token(token_payload)
    return success_response({
        "verification": True,
        "token": token,
        "token_type": "bearer",
        "expires_in": JWT_EXPIRE_HOURS * 3600,
        "user_info": user_info,
        "agent_access": [{"id": d["code"], "name": d["name"]} for d in permissions["agent_whitelist"]],
        "skills_blacklist": permissions["skills_blacklist"],
    })


@router.post("/login")
async def login(request: Request, login_req: LoginRequest):
    result = await verify_login(login_req.username, login_req.password)
    if not result.get("verification"):
        return error_response(401, "用户名或密码错误")
    return await _build_auth_success(result, request)


@router.post("/register")
async def register(request: Request, register_req: RegisterRequest):
    result = await register_user(register_req.username, register_req.password)
    if not result.get("verification"):
        return error_response(400, result.get("message", "注册失败"))
    return await _build_auth_success(result, request)
