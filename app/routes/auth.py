from fastapi import APIRouter, HTTPException
from typing import Any, Dict

from app.dao.user_dao import verify_login
from app.models.auth import LoginRequest
from app.services.auth_service import create_access_token
from app.config import JWT_EXPIRE_HOURS

router = APIRouter()


def success_response(data: Any) -> Dict[str, Any]:
    return {"code": 200, "msg": "success", "data": data}


def error_response(code: int, msg: str) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": {}}


@router.post("/login")
async def login(request: LoginRequest):
    result = await verify_login(request.username, request.password)
    # 暂时使用模拟接口
    result = {
        "verification": True ,
        "user_info":{"user_id":"123","user_name":"小张","department":"后勤部","role":"普通用户"},
        "agent_access":["制度问答"],
        "skills_blacklist":["google"],
    }
    if not result.get("verification"):
        return error_response(401, "用户名或密码错误")

    user_info = result["user_info"]
    agent_access = result.get("agent_access", [])
    skills_blacklist = result.get("skills_blacklist", [])

    token_payload = {
        "user_id": user_info["user_id"],
        "user_name": user_info["user_name"],
        "department": user_info["department"],
        "role": user_info["role"],
        "agent_access": agent_access,
        "skills_blacklist": skills_blacklist,
    }
    token = create_access_token(token_payload)

    return success_response({
        "token": token,
        "token_type": "bearer",
        "expires_in": JWT_EXPIRE_HOURS * 3600,
        "user_info": user_info,
        "agent_access": agent_access,
        "skills_blacklist": skills_blacklist,
    })