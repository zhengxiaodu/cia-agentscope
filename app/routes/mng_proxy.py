import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
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


@router.post("/systemFeedbacks/add")
async def proxy_system_feedback_add(request: Request, user: dict = Depends(current_user)):
    """系统反馈提交：前端 POST /api/systemFeedbacks/add → mng POST /api/feedbacks。"""
    if not MNG_INTENT_URL:
        # 本地开发兜底：仅 AUTH_MOCK=true 时直接返回成功；生产（AUTH_MOCK=false）维持 500
        if _is_auth_mock():
            return {"success": True, "message": None, "data": None}
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    jwt_token = await _get_jwt_from_header(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    # follow_redirects：mng 部分路由注册带尾部斜杠会 307，同源重定向会保留 Authorization 头
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        resp = await client.post(
            f"{MNG_INTENT_URL}/api/feedbacks",
            headers={"Authorization": f"Bearer {jwt_token}"},
            json=body,
        )
        return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.get("/api/portal/notifications")
async def proxy_public_active(request: Request, user: dict = Depends(current_user)):
    """通知接口透传：前端 GET /api/portal/notifications → mng GET api/notifications/public/active，响应原样返回。"""
    if not MNG_INTENT_URL:
        # 本地开发兜底：仅 AUTH_MOCK=true 时读 mock（文件缺失返回空数据封包）；生产维持 500
        if _is_auth_mock():
            return _load_mock_response("public_active.json")
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    jwt_token = await _get_jwt_from_header(request)
    # follow_redirects：mng 部分路由注册带尾部斜杠会 307，同源重定向会保留 Authorization 头
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        resp = await client.get(
            f"{MNG_INTENT_URL}/api/notifications/public/active",
            headers={"Authorization": f"Bearer {jwt_token}"},
            params=dict(request.query_params),
        )
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
