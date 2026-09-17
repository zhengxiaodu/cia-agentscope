"""敏感配置加解密工具。

采用国密 SM4-CBC 对称加密，密文格式 `ENC(<base64(iv + ciphertext)>)`。
- IV 随机生成 16 字节，拼在密文前，整体 base64
- 主密钥 16 字节，由 `CONFIG_DECRYPT_KEY`（32 位 hex）经 `bytes.fromhex` 得到
- `resolve` 识别 `ENC(...)` 前缀才解密，否则原样返回（向后兼容明文）
"""
import base64
import os

from gmssl.sm4 import CryptSM4, SM4_DECRYPT, SM4_ENCRYPT

_IV_LEN = 16  # SM4 分组长度 / IV 长度（字节）
_KEY_LEN = 16  # SM4 密钥长度（字节）


def _check_key(key: bytes) -> None:
    if len(key) != _KEY_LEN:
        raise ValueError(
            f"SM4 主密钥必须为 {_KEY_LEN} 字节，当前 {len(key)} 字节"
        )


def encrypt(plaintext: str, key: bytes) -> str:
    """加密明文，返回 base64(iv + ciphertext) 字符串（不含 ENC 前缀）。"""
    _check_key(key)
    iv = os.urandom(_IV_LEN)
    sm4 = CryptSM4()
    sm4.set_key(key, SM4_ENCRYPT)
    ciphertext = sm4.crypt_cbc(iv, plaintext.encode("utf-8"))
    return base64.b64encode(iv + ciphertext).decode("ascii")


def decrypt(token_b64: str, key: bytes) -> str:
    """解密 base64(iv + ciphertext) 字符串，返回明文。"""
    _check_key(key)
    raw = base64.b64decode(token_b64)
    if len(raw) < _IV_LEN:
        raise ValueError("密文长度不足，无法提取 IV")
    iv, ciphertext = raw[:_IV_LEN], raw[_IV_LEN:]
    sm4 = CryptSM4()
    sm4.set_key(key, SM4_DECRYPT)
    plaintext = sm4.crypt_cbc(iv, ciphertext)
    return plaintext.decode("utf-8")


def resolve(value, key: bytes) -> str:
    """识别 ENC(...) 前缀：是密文则解密，否则原样返回（向后兼容明文）。

    Args:
        value: 配置原始值（可能为 None / 空串 / 明文 / "ENC(...)" 形式密文）
        key: SM4 主密钥（16 字节）。明文模式下可为空 b""。

    Returns:
        解析后的明文字符串。
    """
    if not value:
        return value or ""
    if isinstance(value, str) and value.startswith("ENC(") and value.endswith(")"):
        token = value[4:-1]
        if not key:
            raise RuntimeError(
                "配置项为 ENC(...) 密文，但 CONFIG_DECRYPT_KEY 未配置或为空"
            )
        return decrypt(token, key)
    return value
