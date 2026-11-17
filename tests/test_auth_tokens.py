"""auth_service token 安全测试：refresh token 不得作为接口访问凭证。"""
import os
import runpy
import secrets

import jwt
import pytest

import app.config as config
from app.services.auth_service import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
)
from app.utils.secret_codec import encrypt


@pytest.mark.parametrize("value", [None, "", "  ", "please-change-this-secret"])
def test_rejects_unconfigured_signing_key(monkeypatch, value):
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    if value is not None:
        monkeypatch.setenv("JWT_SECRET", value)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        runpy.run_path(config.__file__)


def test_signing_key_validated_after_decryption(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    key = os.urandom(16)
    monkeypatch.setenv("CONFIG_DECRYPT_KEY", key.hex())
    monkeypatch.setenv("MYSQL_PASSWORD", "")
    for value in ("please-change-this-secret", secrets.token_hex(32)):
        monkeypatch.setenv("JWT_SECRET", f"ENC({encrypt(value, key)})")
        if value == "please-change-this-secret":
            with pytest.raises(RuntimeError, match="JWT_SECRET"):
                runpy.run_path(config.__file__)
        else:
            assert runpy.run_path(config.__file__)["JWT_SECRET"] == value



def test_access_token_decodes_normally():
    token = create_access_token({"user_id": "u1"})
    payload = decode_access_token(token)
    assert payload["user_id"] == "u1"
    assert "type" not in payload  # access token 不带 type 标记


def test_refresh_token_rejected_for_api_auth():
    """修复前：refresh token 可直接通过 decode_access_token（7 天万能凭证）。
    修复后：抛 InvalidTokenError → 依赖层转 401。"""
    refresh = create_refresh_token({"user_id": "u1"})
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(refresh)


def test_refresh_token_still_works_for_refresh_flow():
    """refresh 专用的 decode 通道不受影响。"""
    refresh = create_refresh_token({"user_id": "u1"})
    payload = decode_refresh_token(refresh)
    assert payload["type"] == "refresh"
    assert payload["user_id"] == "u1"


def test_access_token_rejected_by_refresh_decoder():
    """access token 不能走 /refresh（原有行为，回归确认）。"""
    access = create_access_token({"user_id": "u1"})
    with pytest.raises(jwt.InvalidTokenError):
        decode_refresh_token(access)
