"""登录路由：调用 mng 校验 → 存 Redis 权限 → 生成 JWT 返回前端。"""
from fastapi import APIRouter, HTTPException, Request, Depends
from typing import Any, Dict

from app.dao.user_dao import (
    verify_login,
    register as register_user,
    update_name_via_mng,
    update_department_via_mng,
    update_password_via_mng,
)
from app.dependencies import current_user
from app.models.auth import (
    LoginRequest,
    RegisterRequest,
    UpdateNameRequest,
    UpdateDepartmentRequest,
    UpdatePasswordRequest,
)
from app.services.auth_service import (
    create_access_token,
    save_user_permissions,
    get_user_permissions,
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

            # 登录时融合意图/智能体/技能并缓存到 Redis，供 /chat 直接读取
            orchestrator = getattr(request.app.state, "orchestrator_service", None)
            if orchestrator is not None:
                try:
                    await orchestrator.build_and_cache_user_config(
                        user_id=user_id,
                        access_token=access_token,
                        permissions=permissions,
                        redis_client=redis_client,
                    )
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception(
                        f"[auth] 登录时融合并缓存用户 {user_id} 配置失败"
                    )

    # 自己生成 JWT 返回前端（payload 只放基础信息，权限走 Redis 查询）
    token_payload = {
        "user_id": user_id,
        "username": user_info.get("username", ""),
        "name": user_info.get("name", ""),
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
        "agent_access": permissions["agent_whitelist"],
        "skill_blacklist": permissions["skill_blacklist"],
    })


@router.post("/login")
async def login(request: Request, login_req: LoginRequest):
    result = await verify_login(login_req.username, login_req.password)
    if not result.get("verification"):
        return error_response(401, "用户名或密码错误")
    return await _build_auth_success(result, request)


@router.post("/register")
async def register(request: Request, register_req: RegisterRequest):
    result = await register_user(
        username=register_req.username,
        password=register_req.password,
        name=register_req.name,
        department=register_req.department,
    )
    if not result.get("verification"):
        return error_response(400, result.get("message", "注册失败"))
    return await _build_auth_success(result, request)


async def _require_mng_access_token(request: Request, user: dict) -> str:
    """从 Redis 按 user_id 取 mng access_token；取不到则抛 401。"""
    user_id = user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="token 中缺少 user_id")
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis 未就绪")
    perms_data = await get_user_permissions(redis_client, user_id)
    if not perms_data:
        raise HTTPException(status_code=401, detail="用户登录态已过期，请重新登录")
    access_token = perms_data.get("access_token", "")
    if not access_token:
        raise HTTPException(status_code=401, detail="未找到 mng access_token，请重新登录")
    return access_token


async def _build_update_response(request: Request, user: dict, updates: dict) -> dict:
    """重签 JWT + 复用 Redis 的 access_token/permissions，返回与登录一致的结构。

    updates: 需应用到 user_info 的字段变更（如 {"name": ...} / {"department": ...} / {}）。
    """
    user_id = user.get("user_id")
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis 未就绪")
    perms_data = await get_user_permissions(redis_client, user_id)
    if not perms_data:
        raise HTTPException(status_code=401, detail="用户登录态已过期，请重新登录")
    access_token = perms_data.get("access_token", "")
    permissions = perms_data.get("permissions", {}) or {}

    user_info = {
        "id": user_id,
        "username": user.get("username", ""),
        "name": user.get("name", ""),
        "department": user.get("department", ""),
        "role": user.get("role", ""),
    }
    user_info.update(updates)

    token_payload = {
        "user_id": user_id,
        "username": user_info["username"],
        "name": user_info["name"],
        "department": user_info["department"],
        "role": user_info["role"],
    }
    token = create_access_token(token_payload)
    return success_response({
        "verification": True,
        "token": token,
        "token_type": "bearer",
        "expires_in": JWT_EXPIRE_HOURS * 3600,
        "user_info": user_info,
        "agent_access": permissions.get("agent_whitelist", []),
        "skill_blacklist": permissions.get("skill_blacklist", []),
    })


@router.put("/api/auth/me/name")
async def update_name(request: Request, body: UpdateNameRequest, user: dict = Depends(current_user)):
    access_token = await _require_mng_access_token(request, user)
    result = await update_name_via_mng(access_token, body.name)
    if not result.get("success"):
        return error_response(400, result.get("message", "修改姓名失败"))
    return await _build_update_response(request, user, {"name": body.name})


@router.put("/api/auth/me/department")
async def update_department(request: Request, body: UpdateDepartmentRequest, user: dict = Depends(current_user)):
    access_token = await _require_mng_access_token(request, user)
    result = await update_department_via_mng(access_token, body.department)
    if not result.get("success"):
        return error_response(400, result.get("message", "修改部门失败"))
    return await _build_update_response(request, user, {"department": body.department})


@router.put("/api/auth/me/password")
async def update_password(request: Request, body: UpdatePasswordRequest, user: dict = Depends(current_user)):
    access_token = await _require_mng_access_token(request, user)
    result = await update_password_via_mng(access_token, body.old_password, body.new_password)
    if not result.get("success"):
        return error_response(400, result.get("message", "修改密码失败"))
    return await _build_update_response(request, user, {})
