"""登录路由：调用 mng 校验 → 存 Redis 权限 → 生成 JWT 返回前端。"""
import jwt
from fastapi import APIRouter, HTTPException, Request, Depends
from typing import Any, Dict

from app.dao.user_dao import (
    verify_login,
    register as register_user,
    update_name_via_mng,
    update_department_via_mng,
    update_password_via_mng,
    fire_notify_mng_active,
)
from app.dependencies import current_user
from app.models.auth import (
    LoginRequest,
    RegisterRequest,
    UpdateNameRequest,
    UpdateDepartmentRequest,
    UpdatePasswordRequest,
    RefreshRequest,
)
from app.services.auth_service import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    save_user_permissions,
    get_user_permissions,
)
from app.config import JWT_EXPIRE_HOURS, JWT_REFRESH_EXPIRE_DAYS

router = APIRouter()

# 可选技能（前端据此渲染技能开关；目前写死内置技能，描述取自 skill_config.yml）
# 文档/音频解析已改为上传时后台完成（见 file_parse_service），不再作为可选技能
_OPTIONAL_SKILLS = [
    {
        "name": "chart_renderer",
        "nickname": "可视化图表",
        "description": "智能图表渲染技能，根据数据特征自动选择最合适的图表类型并通过工具渲染。",
    },
]


def success_response(data: Any) -> Dict[str, Any]:
    return {"code": 200, "msg": "success", "data": data}


def error_response(code: int, msg: str) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": {}}


def _enrich_agent_access(permissions: dict, agent_definitions: dict) -> None:
    """为 permissions["agent_whitelist"] 每项注入 description。

    description 取自 /api/intents 中对应意图的 definition，
    匹配键为 agent 的 id（与 merge_external_into_memory 白名单匹配一致）。
    未匹配到 definition 的项保持原样（无该字段）。
    对每项做 dict 拷贝后原地写回，避免污染调用方共享对象。
    """
    if not isinstance(agent_definitions, dict) or not agent_definitions:
        return
    whitelist = permissions.get("agent_whitelist")
    if not isinstance(whitelist, list):
        return
    permissions["agent_whitelist"] = [
        {**dict(item), "description": agent_definitions[item["id"]]}
        if isinstance(item, dict) and item.get("id") in agent_definitions
        else item
        for item in whitelist
    ]


async def _build_auth_success(result: dict, request: Request) -> dict:
    """登录/注册成功后的统一处理：存 Redis 权限 → 生成 JWT → 构造前端响应。

    login 与 register 共用此函数，保证返回前端的字段结构逐字段一致。
    """
    user_info = result.get("user_info", {}) or {}
    user_id = user_info.get("id")
    permissions = result.get("permissions", {}) or {}

    # 先签发本系统 JWT，供后续 mng 调用（fetch_external_intents）作为 Authorization
    token_payload = {
        "user_id": user_id,
        "username": user_info.get("username", ""),
        "name": user_info.get("name", ""),
        "department": user_info.get("department", ""),
        "role": user_info.get("role", ""),
    }
    token = create_access_token(token_payload)
    # 同时签发 refresh token（固定有效期 7 天，不滚动刷新）
    refresh_token = create_refresh_token(token_payload)

    # 异步上报 mng 用户活跃（fire-and-forget，失败不影响登录）
    fire_notify_mng_active(token)

    # 将 permissions 按 user_id 存入 Redis，
    # 方便后续 /chat 接口查询用户权限（用于权限过滤）
    if user_id:
        redis_client = getattr(request.app.state, "redis_client", None)
        if redis_client is not None:
            # 登录时融合意图/智能体/技能并缓存到 Redis，供 /chat 直接读取。
            # 先于 permissions 入库执行：成功时从融合结果中取出
            # agent_id → 意图 definition 映射，为 agent_whitelist 注入
            # description 后再存 Redis，使 /refresh、/api/auth/me/* 的
            # agent_access 响应与登录保持一致。失败不阻断登录。
            orchestrator = getattr(request.app.state, "orchestrator_service", None)
            if orchestrator is not None:
                try:
                    fused = await orchestrator.build_and_cache_user_config(
                        user_id=user_id,
                        jwt_token=token,
                        permissions=permissions,
                        redis_client=redis_client,
                    )
                    _enrich_agent_access(
                        permissions, fused.get("agent_definitions", {})
                    )
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception(
                        f"[auth] 登录时融合并缓存用户 {user_id} 配置失败"
                    )

            try:
                await save_user_permissions(redis_client, user_id, permissions)
            except Exception:
                # Redis 写入失败不阻断主流程
                import logging
                logging.getLogger(__name__).exception(
                    f"[auth] 保存用户 {user_id} 权限到 Redis 失败"
                )

    return success_response({
        "verification": True,
        "token": token,
        "token_type": "bearer",
        "expires_in": JWT_EXPIRE_HOURS * 3600,
        "refresh_token": refresh_token,
        "refresh_expires_in": JWT_REFRESH_EXPIRE_DAYS * 86400,
        "user_info": user_info,
        "agent_access": permissions["agent_whitelist"],
        "skill_blacklist": permissions["skill_blacklist"],
        "optional_skills": _OPTIONAL_SKILLS,
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


async def _require_jwt_from_header(request: Request) -> str:
    """从请求头 Authorization 中取出本系统签发的原始 JWT，用于转发给 mng。"""
    authorization = request.headers.get("authorization", "")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少或无效的 Authorization 头")
    jwt_token = authorization.split(" ", 1)[1].strip()
    if not jwt_token:
        raise HTTPException(status_code=401, detail="Authorization 头无效")
    return jwt_token


async def _build_update_response(request: Request, user: dict, updates: dict) -> dict:
    """重签 JWT + 复用 Redis 的 permissions，返回与登录一致的结构。

    updates: 需应用到 user_info 的字段变更（如 {"name": ...} / {"department": ...} / {}）。
    """
    user_id = user.get("user_id")
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis 未就绪")
    perms_data = await get_user_permissions(redis_client, user_id)
    if not perms_data:
        raise HTTPException(status_code=401, detail="用户登录态已过期，请重新登录")
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
        "optional_skills": _OPTIONAL_SKILLS,
    })


@router.put("/api/auth/me/name")
async def update_name(request: Request, body: UpdateNameRequest, user: dict = Depends(current_user)):
    jwt_token = await _require_jwt_from_header(request)
    result = await update_name_via_mng(jwt_token, body.name)
    if not result.get("success"):
        return error_response(400, result.get("message", "修改姓名失败"))
    return await _build_update_response(request, user, {"name": body.name})


@router.put("/api/auth/me/department")
async def update_department(request: Request, body: UpdateDepartmentRequest, user: dict = Depends(current_user)):
    jwt_token = await _require_jwt_from_header(request)
    result = await update_department_via_mng(jwt_token, body.department)
    if not result.get("success"):
        return error_response(400, result.get("message", "修改部门失败"))
    return await _build_update_response(request, user, {"department": body.department})


@router.put("/api/auth/me/password")
async def update_password(request: Request, body: UpdatePasswordRequest, user: dict = Depends(current_user)):
    jwt_token = await _require_jwt_from_header(request)
    result = await update_password_via_mng(jwt_token, body.old_password, body.new_password)
    if not result.get("success"):
        return error_response(400, result.get("message", "修改密码失败"))
    return await _build_update_response(request, user, {})


@router.post("/refresh")
async def refresh_token(request: Request, body: RefreshRequest):
    """凭 refresh_token 换取新的 access token（固定有效期，不滚动刷新）。"""
    try:
        payload = decode_refresh_token(body.refresh_token)
    except jwt.ExpiredSignatureError:
        return error_response(401, "refresh_token 已过期，请重新登录")
    except jwt.InvalidTokenError:
        return error_response(401, "refresh_token 无效")

    user_id = payload.get("user_id")
    if not user_id:
        return error_response(401, "refresh_token 无效")

    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis 未就绪")
    perms_data = await get_user_permissions(redis_client, user_id)
    if not perms_data:
        return error_response(401, "用户登录态已过期，请重新登录")
    permissions = perms_data.get("permissions", {}) or {}

    token_payload = {
        "user_id": user_id,
        "username": payload.get("username", ""),
        "name": payload.get("name", ""),
        "department": payload.get("department", ""),
        "role": payload.get("role", ""),
    }
    token = create_access_token(token_payload)
    user_info = {
        "id": user_id,
        **{k: token_payload[k] for k in ("username", "name", "department", "role")},
    }
    return success_response({
        "verification": True,
        "token": token,
        "token_type": "bearer",
        "expires_in": JWT_EXPIRE_HOURS * 3600,
        "user_info": user_info,
        "agent_access": permissions.get("agent_whitelist", []),
        "skill_blacklist": permissions.get("skill_blacklist", []),
        "optional_skills": _OPTIONAL_SKILLS,
    })
