"""用户登录校验 DAO。

支持两种模式：
- AUTH_MOCK=true：使用内置模拟数据（含 access_token + permissions），便于本地开发
- AUTH_MOCK=false：调用 mng 管理中心进行登录校验，返回结构标准化后供 auth 路由使用

标准返回结构：
    {
        "verification": bool,
        "user_info": {"user_id", "user_name", "department", "role"},
        "access_token": str,        # mng 返回的 access_token
        "permissions": {            # mng 返回的权限
            "agent_whitelist": [{"id","name","code"}, ...],
            "skill_blacklist": [{"id","name","code"}, ...],
        },
    }
验证失败时仅返回 {"verification": False}。
"""
import logging
import os

import httpx

from app.config import MNG_URL

logger = logging.getLogger(__name__)

# 模拟账号数据（含 access_token + permissions，结构与 mng 返回保持一致）
_MOCK_USERS = {
    "zhangsan": {
        "password": "123456",
        "verification": True,
        "user_info": {
            "user_id": "123",
            "user_name": "小张",
            "department": "后勤部",
            "role": "普通用户",
        },
        "access_token": "mock-access-token-zhangsan",
        "permissions": {
            "agent_whitelist": [
                {"id": "123", "name": "制度问答", "code": "zhidu"},
                {"id": "999", "name": "生成PPT智能体", "code": "agent_ppt"},
            ],
            "skill_blacklist": [
                {"id": "456", "name": "博查搜索", "code": "bocha"},
            ],
        },
    },
    "admin": {
        "password": "123456",
        "verification": True,
        "user_info": {
            "user_id": "1",
            "user_name": "管理员",
            "department": "管理部",
            "role": "管理员",
        },
        "access_token": "mock-access-token-admin",
        "permissions": {
            "agent_whitelist": [
                {"id": "123", "name": "制度问答", "code": "zhidu"},
                {"id": "999", "name": "生成PPT智能体", "code": "agent_ppt"},
            ],
            "skill_blacklist": [],
        },
    },
}


async def verify_login_via_mng(username: str, password: str) -> dict:
    """调用 mng 管理中心进行登录校验。

    请求 POST {MNG_URL}/api/auth/login，body: {"username", "password"}
    成功时解析 mng 返回并标准化为内部结构。
    """
    if not MNG_URL:
        logger.error("[user_dao] MNG_URL 未配置，无法调用 mng 登录")
        return {"verification": False}

    url = f"{MNG_URL}/api/auth/login"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"username": username, "password": password},
            )
            if resp.status_code != 200:
                logger.warning(f"[user_dao] mng 登录返回非 200: {resp.status_code}")
                return {"verification": False}

            body = resp.json()
            # mng 返回: {"code":200, "message":"登录成功", "data":{...}}
            if body.get("code") != 200:
                logger.warning(f"[user_dao] mng 登录业务失败: {body.get('message')}")
                return {"verification": False}

            data = body.get("data", {}) or {}
            return {
                "verification": True,
                "user_info": data.get("user_info", {}),
                "access_token": data.get("access_token", ""),
                "permissions": data.get("permissions", {}),
            }
    except Exception as e:
        logger.exception(f"[user_dao] 调用 mng 登录服务失败: {e}")
        return {"verification": False}


async def verify_login(username: str, password: str) -> dict:
    """验证登录凭据。

    当 AUTH_MOCK=true 时使用内置模拟数据; 否则请求 mng 管理中心。
    """
    if os.getenv("AUTH_MOCK", "true").lower() == "true":
        user = _MOCK_USERS.get(username)
        if user and user["password"] == password:
            return {
                "verification": True,
                "user_info": user["user_info"],
                "access_token": user["access_token"],
                "permissions": user["permissions"],
            }
        return {"verification": False}

    return await verify_login_via_mng(username, password)
