import logging
from typing import Optional

import jwt
from fastapi import HTTPException, Header

from app.services.auth_service import decode_access_token

logger = logging.getLogger(__name__)


async def current_user(authorization: Optional[str] = Header(None)) -> dict:
    """FastAPI 依赖: 从 Authorization: Bearer <token> 解析当前用户。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少或无效的 Authorization 头")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期, 请重新登录")
    except jwt.InvalidSignatureError:
        # 签名校验失败：多为多副本间 JWT_SECRET 不一致（旧 pod 签发的 token
        # 被新 pod 校验）或 token 被篡改。
        logger.warning(
            "JWT 签名校验失败：JWT_SECRET 可能不一致或 token 被篡改, token_prefix=%s",
            token[:30],
        )
        raise HTTPException(status_code=401, detail="登录凭证无效")
    except jwt.DecodeError:
        # token 格式损坏/截断：多为前端存储或传输异常。
        logger.warning(
            "JWT 格式损坏：无法解码, token_prefix=%s", token[:30],
        )
        raise HTTPException(status_code=401, detail="登录凭证无效")
    except jwt.InvalidTokenError as exc:
        # 其余无效场景（含 auth_service 主动抛的 "refresh token 不能用于接口鉴权"）。
        logger.warning("JWT 无效: %s", exc)
        raise HTTPException(status_code=401, detail="登录凭证无效")