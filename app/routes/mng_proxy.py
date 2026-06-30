from fastapi import APIRouter, Depends, HTTPException, Request
import httpx

from app.config import MNG_INTENT_URL
from app.dependencies import current_user
from app.services.auth_service import get_user_permissions

router = APIRouter()


async def _get_access_token(request: Request, user: dict) -> str:
    """从 redis 按 user_id 取 mng access_token；取不到则抛 401。"""
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


@router.get("/api/ui/presentation/cards")
async def proxy_card_configs(request: Request, user: dict = Depends(current_user)):
    if not MNG_INTENT_URL:
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    access_token = await _get_access_token(request, user)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{MNG_INTENT_URL}/ui/presentation/cards",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return resp.json()


@router.get("/api/ui/presentation/custom-components")
async def proxy_custom_component_configs(request: Request, user: dict = Depends(current_user)):
    if not MNG_INTENT_URL:
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    access_token = await _get_access_token(request, user)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{MNG_INTENT_URL}/ui/presentation/custom-components",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return resp.json()
