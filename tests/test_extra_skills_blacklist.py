"""附加技能 skill_blacklist 越权过滤单测。

背景：融合阶段（mng_service）仅过滤 agent 绑定技能，前端传入的请求级
附加技能（extra_skill_names）此前未过黑名单校验，存在绕过越权风险。
修复：workspace_assembler._load_skill_blacklist 读取 Redis 用户权限中的
skill_blacklist code 集合，装配时过滤 extra_skills。
"""
import json
from unittest.mock import AsyncMock

import pytest

from app.services.workspace_assembler import _load_skill_blacklist


def _redis_with_permissions(permissions: dict | None):
    redis = AsyncMock()
    if permissions is None:
        redis.get = AsyncMock(return_value=None)
    else:
        payload = json.dumps({"permissions": permissions}, ensure_ascii=False)
        redis.get = AsyncMock(return_value=payload.encode("utf-8"))
    return redis


@pytest.mark.asyncio
async def test_load_blacklist_dict_items():
    redis = _redis_with_permissions({
        "agent_whitelist": [],
        "skill_blacklist": [
            {"id": "456", "name": "博查搜索", "code": "bocha_search"},
            {"id": "789", "name": "Excel", "code": "excel_xlsx"},
        ],
    })
    codes = await _load_skill_blacklist(redis, "u1")
    assert codes == {"bocha_search", "excel_xlsx"}
    redis.get.assert_awaited_once_with("user_permissions:u1")


@pytest.mark.asyncio
async def test_load_blacklist_plain_string_items():
    redis = _redis_with_permissions({
        "skill_blacklist": ["bocha_search", "word_docx"],
    })
    assert await _load_skill_blacklist(redis, "u1") == {"bocha_search", "word_docx"}


@pytest.mark.asyncio
async def test_load_blacklist_empty_or_missing():
    # 无权限缓存（未登录/过期）→ 空集合
    assert await _load_skill_blacklist(_redis_with_permissions(None), "u1") == set()
    # 黑名单为空列表
    redis = _redis_with_permissions({"skill_blacklist": []})
    assert await _load_skill_blacklist(redis, "u1") == set()
    # permissions 缺失 skill_blacklist 字段
    redis = _redis_with_permissions({})
    assert await _load_skill_blacklist(redis, "u1") == set()


@pytest.mark.asyncio
async def test_load_blacklist_no_client_or_user():
    assert await _load_skill_blacklist(None, "u1") == set()
    assert await _load_skill_blacklist(_redis_with_permissions({"skill_blacklist": ["a"]}), "") == set()


@pytest.mark.asyncio
async def test_load_blacklist_redis_error_degrades():
    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    # Redis 异常仅降级为空集合（不过滤），不抛出
    assert await _load_skill_blacklist(redis, "u1") == set()
