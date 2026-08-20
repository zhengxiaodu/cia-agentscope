"""安全敏感内容检测服务。

对接统一敏感检测接口（本地词典 + MiniCPM5 语义模型）：
- 命中（code=0 且 hasSensitiveWord=true）返回 blocked=True 及风险原因
- 任何异常（地址未配置/网络错误/超时/HTTP 非 2xx/JSON 解析失败/code!=0）兜底放行
- 每次调用通过 Langfuse span 记录输入、输出、耗时与阶段
"""
import logging
import time
from typing import Any, Dict

import httpx

from app.config import (
    SENSITIVE_SERVICE_URL,
    SENSITIVE_THRESHOLD,
    SENSITIVE_TIMEOUT,
)
from app.services.langfuse_service import get_current_langfuse

logger = logging.getLogger(__name__)

# 兜底放行时的统一返回结构（source 标记跳过原因）
_FALLBACK_RESULT: Dict[str, Any] = {
    "blocked": False,
    "reason": "",
    "category": "",
    "sensitive_words": [],
    "source": "client-skip",
    "raw": None,
}


def _fallback(source: str, reason: str = "") -> Dict[str, Any]:
    """构造兜底放行结果（复制避免共享可变状态）。"""
    result = dict(_FALLBACK_RESULT)
    result["source"] = source
    result["reason"] = reason
    return result


def build_message_replace_event(result: Dict[str, Any], stage: str) -> Dict[str, Any]:
    """根据检测结果构造 message_replace 事件（命中时前端替换消息并停止接收）。"""
    reason = str(result.get("reason", "") or "").strip()
    message = "您的内容涉及敏感词，请修改提问"
    if reason:
        message = f"您的内容涉及敏感词（{reason}），请修改提问"
    return {
        "type": "message_replace",
        "message": message,
        "reason": reason,
        "stage": stage,
    }


def _blocked_from_response(data: dict) -> Dict[str, Any]:
    """从服务正常响应（code=0）构造检测结果。"""
    semantic = data.get("semantic") or {}
    blocked = bool(data.get("hasSensitiveWord"))
    return {
        "blocked": blocked,
        "reason": str(semantic.get("reason", "")),
        "category": str(semantic.get("category", "")),
        "sensitive_words": list(data.get("sensitiveWords") or []),
        "source": str(semantic.get("source", "")),
        "raw": data,
    }


async def check_sensitive(text: str, stage: str = "input") -> Dict[str, Any]:
    """调用敏感检测服务检测文本。

    Args:
        text: 待检测文本。为空时直接放行（服务约定空文本未命中）。
        stage: 调用阶段标记（"input" 用户输入检测 / "output" 编排输出检测），
            仅用于 Langfuse 埋点与日志。

    Returns:
        {"blocked": bool, "reason": str, "category": str,
         "sensitive_words": list, "source": str, "raw": dict|None}
        任何异常均返回 blocked=False（兜底放行）。
    """
    # 服务未配置：跳过检测
    if not SENSITIVE_SERVICE_URL:
        return _fallback("client-skip")

    # 空文本：服务约定返回未命中，直接放行省一次调用
    if not text or not text.strip():
        return _fallback("client-skip")

    langfuse_service = get_current_langfuse()
    span_ctx = (
        langfuse_service.start_span(
            "sensitive-check",
            input={"text": text, "threshold": SENSITIVE_THRESHOLD, "stage": stage},
        )
        if langfuse_service and langfuse_service.enabled
        else None
    )

    t0 = time.perf_counter()
    result: Dict[str, Any]
    try:
        if span_ctx is None:
            result = await _do_check(text)
        else:
            with span_ctx as span:
                result = await _do_check(text)
                if span:
                    try:
                        span.update(
                            output={
                                "blocked": result["blocked"],
                                "reason": result["reason"],
                                "category": result["category"],
                                "sensitive_words": result["sensitive_words"],
                                "source": result["source"],
                                "raw": result["raw"],
                            },
                            metadata={
                                "latency_ms": int((time.perf_counter() - t0) * 1000),
                                "stage": stage,
                            },
                        )
                    except Exception:
                        pass
    except Exception as e:
        # _do_check 内部已兜底，此层防御性捕获保证绝不抛出
        logger.warning(
            "[sensitive_service] 检测异常兜底放行 stage=%s: %s", stage, e
        )
        result = _fallback("client-fallback", str(e))
    return result


async def _do_check(text: str) -> Dict[str, Any]:
    """实际 HTTP 调用与响应解析（含全部兜底分支）。"""
    try:
        async with httpx.AsyncClient(timeout=SENSITIVE_TIMEOUT) as client:
            resp = await client.post(
                SENSITIVE_SERVICE_URL,
                json={"text": text, "threshold": SENSITIVE_THRESHOLD},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("[sensitive_service] 检测超时（%ss），兜底放行", SENSITIVE_TIMEOUT)
        return _fallback("client-fallback", "timeout")
    except Exception as e:
        logger.warning("[sensitive_service] 检测请求失败，兜底放行: %s", e)
        return _fallback("client-fallback", str(e))

    # JSON 已解析成功，但结构异常仍需兜底
    if not isinstance(data, dict):
        logger.warning("[sensitive_service] 响应非对象，兜底放行: %r", data)
        return _fallback("client-fallback", "invalid-response")

    code = data.get("code")
    if code != 0:
        # 业务错误码：40001 请求为空/缺 text、50001 模型异常等，放行并记录
        logger.warning(
            "[sensitive_service] 检测服务返回业务错误 code=%s，兜底放行", code
        )
        return _fallback("client-fallback", f"code={code}")

    if "hasSensitiveWord" not in data:
        logger.warning("[sensitive_service] 响应缺少 hasSensitiveWord 字段，兜底放行")
        return _fallback("client-fallback", "missing-field")

    return _blocked_from_response(data)
