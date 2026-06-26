from datetime import datetime, timedelta, timezone

import jwt

from app.config import JWT_ALGORITHM, JWT_SECRET, JWT_EXPIRE_HOURS


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