import json
import logging
from datetime import datetime, timedelta, timezone

import jwt

from app.config import JWT_ALGORITHM, JWT_SECRET, JWT_EXPIRE_HOURS

logger = logging.getLogger(__name__)

# Redis key 前缀：用户权限数据
_REDIS_KEY_PERMISSIONS = "user_permissions:{user_id}"
# 权限数据在 Redis 中的默认 TTL（秒），与 JWT 过期时间一致
_PERMISSIONS_TTL = JWT_EXPIRE_HOURS * 3600


def create_access_token(payload: dict, expire_hours: int = JWT_EXPIRE_HOURS) -> str:
    """生成 JWT, 默认按 .env 中 JWT_EXPIRE_HOURS 过期。"""
    now = datetime.now(timezone.utc)
    body = payload.copy()
    body["iat"] = int(now.timestamp())
    body["exp"] = int((now + timedelta(hours=expire_hours)).timestamp())
    return jwt.encode(body, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解析 JWT; 过期或签名错误时抛出 jwt 异常。"""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


async def save_user_permissions(redis_client, user_id: str, access_token: str, permissions: dict) -> None:
    """将用户的 mng access_token 和 permissions 存入 Redis。

    Args:
        redis_client: redis.asyncio 客户端
        user_id: 用户唯一标识（来自 mng 返回的 user_info）
        access_token: mng 系统返回的 access_token
        permissions: mng 系统返回的 permissions 对象
            {"agent_whitelist": [...], "skill_blacklist": [...]}
    """
    key = _REDIS_KEY_PERMISSIONS.format(user_id=user_id)
    value = json.dumps({
        "access_token": access_token,
        "permissions": permissions,
    }, ensure_ascii=False)
    ttl = _PERMISSIONS_TTL
    await redis_client.set(key, value.encode("utf-8"), ex=ttl)
    logger.info(f"[auth_service] 用户权限已存入 Redis: key={key}, ttl={ttl}s")


async def get_user_permissions(redis_client, user_id: str) -> dict | None:
    """从 Redis 获取用户的 mng access_token 和 permissions。

    Returns:
        成功返回 {"access_token": str, "permissions": dict}，
        不存在或解析失败返回 None。
    """
    key = _REDIS_KEY_PERMISSIONS.format(user_id=user_id)
    raw = await redis_client.get(key)
    if raw is None:
        logger.debug(f"[auth_service] Redis 中未找到用户权限: key={key}")
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        logger.exception(f"[auth_service] 解析用户权限失败: key={key}")
        return None