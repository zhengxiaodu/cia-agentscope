"""Agent 事件的 JSON 序列化，仅对代理字符编码异常提供传输/落库兜底。"""

import json
import logging
import re

from pydantic import BaseModel
from pydantic_core import PydanticSerializationError

logger = logging.getLogger(__name__)

# JSON 字符串值中允许的控制字符：只保留换行、回车、制表符；
# 其余 \x00-\x1f 范围字符（含 \x00 null、\x07 bell、\x1b escape 等）
# 会破坏 Python 3.13 上游 JSON 解析器在嵌套转义时的正常行为，
# 导致 API 服务端返回 "Unterminated string" 400 错误。
# 这些字符可能来自 OpenSandbox bash 命令的原始输出（如 ANSI 彩色码被
# SDK 截断、二进制文件片段混入、printf 转义等）。
_ALLOWED_CTRL = frozenset("\n\r\t")

# 匹配"孤立代理"的正则（高代理段 \uD800-\uDBFF 后未跟低代理 \uDC00-\uDFFF，
# 或低代理独立出现）。
_SURROGATE_RE = re.compile(r"[\uD800-\uDBFF](?![\uDC00-\uDFFF])|[\uDC00-\uDFFF](?<![\uD800-\uDBFF])")


def _fix_str_surrogates(s: str) -> str:
    """还原代理对为完整字符，孤立代理替换为 U+FFFD。

    流式 chunk 边界可能把 emoji 的 UTF-16 代理对切成两半，各自独立
    解码后成为孤立代理字符，无法编码为 UTF-8（pymysql 落库、JSON
    序列化均会抛 UnicodeEncodeError）。绕道 UTF-16 往返让相邻的
    代理对（高代理 + 低代理）重组为完整 emoji 字符，残余孤立代理
    降级为 U+FFFD。
    """
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _strip_control_chars(s: str) -> str:
    """移除 JSON 不接受的控制字符，仅保留 \n \r \t。

    JSON 规范 (RFC 8259) 允许字符串值中出现 U+0020 之外的可见字符、
    以及 U+0008/U+0009/U+000A/U+000C/U+000D；但在实际生产中：
    - U+0000 (null) 会让 C 实现的 JSON 解析器直接停止解析，
      常见症状就是 "Unterminated string"
    - U+001B (ESC) 出现在 ANSI 颜色码里，如果被 SDK 截断成半截
      或夹杂在其他字符之间，同样会破坏 JSON 序列化
    - 其他 \x01-\x1f 字符（除 \n\r\t）一般出现在 bash 输出的
      二进制片段中，对 LLM 毫无意义但会导致请求 400
    为安全起见，只保留 \n\r\t，其余 \x00-\x1f 范围字符全部剔除。
    """
    out = []
    for ch in s:
        cp = ord(ch)
        if cp <= 0x1F and ch not in _ALLOWED_CTRL:
            # 这些控制字符直接丢弃，不做替换——替换为 \uXXXX 反而增加 JSON
            # 体积且对 LLM 无任何信息增量
            continue
        out.append(ch)
    return "".join(out)


def sanitize_surrogates(obj):
    """递归清洗 str/list/dict 中的孤立代理字符，其余类型原样返回。"""
    if isinstance(obj, str):
        return _fix_str_surrogates(obj)
    if isinstance(obj, list):
        return [sanitize_surrogates(v) for v in obj]
    if isinstance(obj, dict):
        return {k: sanitize_surrogates(v) for k, v in obj.items()}
    return obj


def sanitize_text(s: str) -> str:
    """对可能来自 bash/工具输出的任意文本做全链路清洗。

    顺序：先清理孤立代理（surrogatepass 解码能让 UTF-16 代理对
    重新配对），再剔除控制字符。两次处理独立、可叠加。

    Returns:
        清洗后的安全字符串；保证可被 json.dumps 正确序列化，
        不会触发 Python 3.13 "Unterminated string" 错误。
    """
    if not isinstance(s, str) or not s:
        return s
    # 第一关：代理字符（emoji 被流式 chunk 切断）
    s = _fix_str_surrogates(s)
    # 第二关：JSON 不接受的控制字符（bash ANSI 码、二进制片段、null 字节）
    s = _strip_control_chars(s)
    return s


def sanitize_text_recursive(obj):
    """递归版本的 sanitize_text，用于 list/dict 容器。"""
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, list):
        return [sanitize_text_recursive(v) for v in obj]
    if isinstance(obj, dict):
        return {k: sanitize_text_recursive(v) for k, v in obj.items()}
    return obj


def serialize_agent_event(event: BaseModel) -> str:
    try:
        return event.model_dump_json()
    except PydanticSerializationError as exc:
        # Pydantic 将 UnicodeEncodeError 包装为序列化异常；其他错误仍向上传递。
        if "UnicodeEncodeError" not in str(exc) or "surrogates not allowed" not in str(exc):
            raise
        # 保留 JSON 模式的字段序列化规则，将代理项转义，避免删除跨 chunk 字符。
        payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"))
        logger.warning("Agent event Unicode escape fallback: event_class=%s", type(event).__name__)
        return payload
