import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
import httpx

from app.config import MNG_INTENT_URL
from app.dependencies import current_user

router = APIRouter()

# 本地 mock 组件配置目录（AUTH_MOCK=true 且 MNG_INTENT_URL 为空时生效）
_MOCK_DIR = Path(__file__).resolve().parents[2] / "config" / "mock"


def _is_auth_mock() -> bool:
    """与 user_dao 的 AUTH_MOCK 判定保持同一模式。"""
    return os.getenv("AUTH_MOCK", "true").lower() == "true"


def _load_mock_response(filename: str) -> dict:
    """读取本地 mock 组件配置；文件不存在时返回空列表封包。"""
    mock_file = _MOCK_DIR / filename
    if not mock_file.exists():
        return {"success": True, "message": None, "data": []}
    return json.loads(mock_file.read_text(encoding="utf-8"))


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
        # 本地开发兜底：仅 AUTH_MOCK=true 时读 mock；生产（AUTH_MOCK=false）维持 500
        if _is_auth_mock():
            return _load_mock_response("presentation_cards.json")
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    jwt_token = await _get_jwt_from_header(request)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{MNG_INTENT_URL}/api/presentation/cards/all",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        return resp.json()


@router.get("/api/presentation/custom-components")
async def proxy_custom_component_configs(request: Request, user: dict = Depends(current_user)):
    if not MNG_INTENT_URL:
        # 本地开发兜底：仅 AUTH_MOCK=true 时读 mock；生产（AUTH_MOCK=false）维持 500
        if _is_auth_mock():
            return _load_mock_response("presentation_custom_components.json")
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    jwt_token = await _get_jwt_from_header(request)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{MNG_INTENT_URL}/api/presentation/custom/all",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        return resp.json()
