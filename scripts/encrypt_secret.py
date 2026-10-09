"""敏感配置加密辅助脚本。

用法：
    CONFIG_DECRYPT_KEY=<32位hex> python scripts/encrypt_secret.py "<明文>"

输出：
    ENC(<base64密文>)   —— 直接粘贴到 .env 对应配置项的值

主密钥生成：
    python -c "import secrets; print(secrets.token_hex(16))"
"""
import os
import sys

# 让脚本能从仓库根目录直接运行时找到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.utils.secret_codec import encrypt  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print('用法: CONFIG_DECRYPT_KEY=<32位hex> python scripts/encrypt_secret.py "<明文>"', file=sys.stderr)
        sys.exit(2)

    plaintext = sys.argv[1]
    key_hex = os.getenv("CONFIG_DECRYPT_KEY", "")
    if not key_hex:
        print("错误：环境变量 CONFIG_DECRYPT_KEY 未设置", file=sys.stderr)
        sys.exit(2)

    try:
        key = bytes.fromhex(key_hex)
    except ValueError as e:
        print(f"错误：CONFIG_DECRYPT_KEY 不是合法 hex：{e}", file=sys.stderr)
        sys.exit(2)

    token = encrypt(plaintext, key)
    print(f"ENC({token})")


if __name__ == "__main__":
    main()
