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

from app.config import MNG_AUTH_URL

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


def _normalize_user_info(raw: dict) -> dict:
    """标准化 mng 返回的 user_info，确保包含内部统一的 user_id 字段。

    mng 真实返回的 user_info 主键字段名为 `id`（非 `user_id`），
    这里映射为内部统一键 `user_id`，下游（auth 存 Redis / JWT payload /
    chat 路由 / orchestrator）一律按 `user_id` 消费。
    """
    if not raw:
        return {}
    normalized = dict(raw)
    if not normalized.get("user_id"):
        uid = normalized.get("id") or normalized.get("userId") or normalized.get("uid")
        if uid is not None:
            normalized["user_id"] = str(uid)
    return normalized


async def verify_login_via_mng(username: str, password: str) -> dict:
    """调用 mng 管理中心进行登录校验。

    请求 POST {MNG_AUTH_URL}/api/auth/login，body: {"username", "password"}
    成功时解析 mng 返回并标准化为内部结构。
    """
    if not MNG_AUTH_URL:
        logger.error("[user_dao] MNG_AUTH_URL 未配置，无法调用 mng 登录")
        return {"verification": False}

    url = f"{MNG_AUTH_URL}/api/auth/login"
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
                "user_info": _normalize_user_info(data.get("user_info", {})),
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


async def register_via_mng(username: str, password: str) -> dict:
    """调用 mng 管理中心注册。

    请求 POST {MNG_AUTH_URL}/api/auth/register, body: {"username", "password"}
    成功时把 mng 返回标准化为与登录一致的内部结构。
    失败时返回 {"verification": False, "message": <mng message 或默认>}。
    """
    if not MNG_AUTH_URL:
        logger.error("[user_dao] MNG_AUTH_URL 未配置，无法调用 mng 注册")
        return {"verification": False, "message": "MNG_AUTH_URL 未配置"}

    url = f"{MNG_AUTH_URL}/api/auth/register"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"username": username, "password": password},
            )
            if resp.status_code != 200:
                logger.warning(f"[user_dao] mng 注册返回非 200: {resp.status_code}")
                return {"verification": False, "message": "注册失败"}

            body = resp.json()
            # mng 返回: {"code":200, "message":"注册成功", "data":{...}}
            if body.get("code") != 200:
                msg = body.get("message", "注册失败")
                logger.warning(f"[user_dao] mng 注册业务失败: {msg}")
                return {"verification": False, "message": msg}

            data = body.get("data", {}) or {}
            return {
                "verification": True,
                "user_info": _normalize_user_info(data.get("user_info", {})),
                "access_token": data.get("access_token", ""),
                "permissions": data.get("permissions", {}),
            }
    except Exception as e:
        logger.exception(f"[user_dao] 调用 mng 注册服务失败: {e}")
        return {"verification": False, "message": "注册服务异常"}


async def register(username: str, password: str) -> dict:
    """注册用户。AUTH_MOCK=true 时返回模拟新用户；否则调用 mng 注册。"""
    if os.getenv("AUTH_MOCK", "true").lower() == "true":
        # 模拟注册成功：返回新账号结构（空权限）
        return {
            "verification": True,
            "user_info": {
                "user_id": username,
                "user_name": username,
                "department": "",
                "role": "普通用户",
            },
            "access_token": f"mock-access-token-{username}",
            "permissions": {"agent_whitelist": [], "skills_blacklist": []},
        }
    return await register_via_mng(username, password)
