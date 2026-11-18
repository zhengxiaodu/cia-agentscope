"""会话归属校验测试：他人会话 ID 不得用于 /chat（越权修复）。"""
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services.session_service import SessionService


def _svc(reserved_owner):
    """reserved_owner：reserve_session 返回的属主与删除状态。"""
    dao = AsyncMock()
    dao.reserve_session = AsyncMock(return_value=reserved_owner)
    return SessionService(dao), dao


@pytest.mark.asyncio
async def test_foreign_session_id_rejected():
    """session 属于他人 → 403（修复前直接透传）。"""
    svc, _ = _svc({"user_id": "owner_a", "deleted_at": None})
    with pytest.raises(HTTPException) as exc:
        await svc.get_or_create_session("s1", "attacker_b")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_own_session_id_allowed():
    svc, dao = _svc({"user_id": "u1", "deleted_at": None})
    assert await svc.get_or_create_session("s1", "u1") == "s1"
    dao.reserve_session.assert_awaited_once_with("s1", "u1")


@pytest.mark.asyncio
async def test_unknown_session_id_allowed():
    """sessions 表无记录 → 首次使用原子登记属主后放行。"""
    svc, dao = _svc({"user_id": "u1", "deleted_at": None})
    assert await svc.get_or_create_session("fresh_id", "u1") == "fresh_id"
    dao.reserve_session.assert_awaited_once_with("fresh_id", "u1")


@pytest.mark.asyncio
async def test_no_session_id_creates_new():
    svc, dao = _svc({"user_id": "u1", "deleted_at": None})
    new_id = await svc.get_or_create_session(None, "u1")
    assert new_id and isinstance(new_id, str) and len(new_id) == 32
    dao.reserve_session.assert_awaited_once_with(new_id, "u1")


@pytest.mark.asyncio
async def test_deleted_session_id_rejected():
    """软删会话 → 403，与『不存在』区分。"""
    svc, _ = _svc({"user_id": "u1", "deleted_at": "2026-01-01 00:00:00"})
    with pytest.raises(HTTPException) as exc:
        await svc.get_or_create_session("s1", "u1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_legacy_claim_conflict_rejected():
    """首次认领时发现其他用户的上传记录（历史遗留窗口）→ PermissionError 转 403。"""
    dao = AsyncMock()
    dao.reserve_session = AsyncMock(side_effect=PermissionError("该会话已有其他用户的上传记录"))
    svc = SessionService(dao)
    with pytest.raises(HTTPException) as exc:
        await svc.get_or_create_session("s1", "u1")
    assert exc.value.status_code == 403
