"""fetch_external_intents 禁用意图（status=0）过滤测试。

mock httpx.AsyncClient 构造 /api/intents 返回，验证：
status=0 被剔除；status=1、字段缺失、None 视为启用保留（旧版兼容）。
"""
from unittest.mock import MagicMock

import pytest

import app.services.mng_service as mng_service
from app.services.mng_service import fetch_external_intents


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """替代 httpx.AsyncClient：get 永远返回预置响应。"""

    response = _FakeResponse()

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return _FakeAsyncClient.response


@pytest.fixture
def mng_configured(monkeypatch):
    """配置 MNG_INTENT_URL 并替换 httpx.AsyncClient。"""
    monkeypatch.setattr(mng_service, "MNG_INTENT_URL", "http://mng.test")
    monkeypatch.setattr(mng_service.httpx, "AsyncClient", _FakeAsyncClient)


def _intent(code, status="__missing__"):
    item = {"id": 1, "name": code, "intentCode": code}
    if status != "__missing__":
        item["status"] = status
    return item


async def _fetch(data):
    _FakeAsyncClient.response = _FakeResponse(
        payload={"code": 200, "data": data}
    )
    return await fetch_external_intents("jwt-token")


@pytest.mark.asyncio
async def test_disabled_intent_filtered(mng_configured):
    """status=0 剔除；status=1 与异常值（非 0）保留。"""
    result = await _fetch([
        _intent("a", 1),
        _intent("b", 0),
        _intent("c", 0),
        _intent("d", 2),   # 非 0 非 1 → 按"非 0"保留
    ])

    assert [x["intentCode"] for x in result] == ["a", "d"]


@pytest.mark.asyncio
async def test_missing_status_treated_as_enabled(mng_configured):
    """旧版 mng 无 status 字段 → 全部保留，行为不变。"""
    result = await _fetch([_intent("a"), _intent("b")])

    assert [x["intentCode"] for x in result] == ["a", "b"]


@pytest.mark.asyncio
async def test_none_status_treated_as_enabled(mng_configured):
    """status=None → 视为启用保留。"""
    result = await _fetch([_intent("a", None), _intent("b", 0)])

    assert [x["intentCode"] for x in result] == ["a"]


@pytest.mark.asyncio
async def test_all_disabled_returns_empty(mng_configured):
    result = await _fetch([_intent("a", 0), _intent("b", 0)])
    assert result == []


@pytest.mark.asyncio
async def test_non_dict_items_kept_as_is(mng_configured):
    """非 dict 条目（脏数据）不含 status 逻辑，原样透传（与旧行为一致）。"""
    result = await _fetch([_intent("a", 1), "bad-item"])
    assert len(result) == 2
    assert result[1] == "bad-item"


@pytest.mark.asyncio
async def test_non_list_data_returns_empty(mng_configured):
    """data 非列表 → []（原有行为不回归）。"""
    _FakeAsyncClient.response = _FakeResponse(
        payload={"code": 200, "data": {"not": "a list"}}
    )
    assert await fetch_external_intents("jwt-token") == []


@pytest.mark.asyncio
async def test_business_failure_returns_empty(mng_configured):
    """业务 code != 200 → []。"""
    _FakeAsyncClient.response = _FakeResponse(
        payload={"code": 500, "message": "err"}
    )
    assert await fetch_external_intents("jwt-token") == []


@pytest.mark.asyncio
async def test_mng_url_not_configured(monkeypatch):
    """MNG_INTENT_URL 未配置 → []（原有行为不回归）。"""
    monkeypatch.setattr(mng_service, "MNG_INTENT_URL", "")
    assert await fetch_external_intents("jwt-token") == []


@pytest.mark.asyncio
async def test_empty_jwt_returns_empty(mng_configured):
    assert await fetch_external_intents("") == []
