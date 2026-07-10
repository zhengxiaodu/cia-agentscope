from fastapi import APIRouter, Depends, HTTPException, Request
import httpx

from app.config import MNG_INTENT_URL
from app.dependencies import current_user

router = APIRouter()


async def _get_jwt_from_header(request: Request) -> str:
    """从请求头 Authorization 中取出本系统签发的原始 JWT，用于转发给 mng。"""
    authorization = request.headers.get("authorization", "")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少或无效的 Authorization 头")
    jwt_token = authorization.split(" ", 1)[1].strip()
    if not jwt_token:
        raise HTTPException(status_code=401, detail="Authorization 头无效")
    return jwt_token


@router.get("/api/presentation/cards")
async def proxy_card_configs(request: Request, user: dict = Depends(current_user)):
    if not MNG_INTENT_URL:
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    jwt_token = await _get_jwt_from_header(request)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{MNG_INTENT_URL}/api/presentation/cards",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        return resp.json()


@router.get("/api/presentation/custom-components")
async def proxy_custom_component_configs(request: Request, user: dict = Depends(current_user)):
    if not MNG_INTENT_URL:
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    jwt_token = await _get_jwt_from_header(request)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{MNG_INTENT_URL}/api/presentation/custom-components",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        return resp.json()
