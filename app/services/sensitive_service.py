"""安全敏感内容检测服务。

对接统一敏感检测服务（本地词典 + MiniCPM5 语义模型），
服务地址取基础形式（IP+端口），具体接口路径由本模块按场景拼接：
- 输入阶段走纯词典检测（{base}/sensitive/dict-check，快，无语义模型开销）
- 输出阶段走完整检测（{base}/sensitive/check，词典 + 语义）
- 命中（code=0 且 hasSensitiveWord=true）返回 blocked=True 及风险原因
- 任何异常（开关关闭/地址未配置/网络错误/超时/HTTP 非 2xx/JSON 解析失败/code!=0）兜底放行
- 每次调用通过 Langfuse span 记录输入、输出、耗时与阶段
"""
import logging
import time
from typing import Any, Dict, Optional

import httpx

from app.config import (
    SENSITIVE_ENABLED,
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
    "hit_sources": [],
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


def _service_base() -> str:
    """返回敏感检测服务基础地址（IP+端口形式，去掉尾部斜杠）。

    兼容遗留的完整路径写法（…/sensitive/check 或 …/sensitive/dict-check）：
    剥掉旧后缀并告警，保证升级 .env 前后行为一致。
    调用时读取而非模块级常量，保证测试 monkeypatch SENSITIVE_SERVICE_URL 生效。
    """
    base = SENSITIVE_SERVICE_URL.rstrip("/")
    for suffix in ("/sensitive/check", "/sensitive/dict-check"):
        if base.endswith(suffix):
            logger.warning(
                "[sensitive_service] SENSITIVE_SERVICE_URL 建议只配置 IP+端口基础地址，"
                "已自动剥离遗留路径后缀 %s", suffix,
            )
            base = base[: -len(suffix)]
            break
    return base


async def _post_sensitive(url: str, payload: dict) -> Optional[dict]:
    """实际 HTTP 调用与响应校验，返回服务响应 dict；任何异常返回 None（由调用方兜底）。

    区分不了具体失败原因时返回 None；调用方根据 None 构造 client-fallback。
    注意：网络/超时/业务错误码在此函数内已记日志并返回 None。
    """
    try:
        async with httpx.AsyncClient(timeout=SENSITIVE_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("[sensitive_service] 检测超时（%ss），兜底放行", SENSITIVE_TIMEOUT)
        return None
    except Exception as e:
        logger.warning("[sensitive_service] 检测请求失败，兜底放行: %s", e)
        return None

    # JSON 已解析成功，但结构异常仍需兜底
    if not isinstance(data, dict):
        logger.warning("[sensitive_service] 响应非对象，兜底放行: %r", data)
        return None

    code = data.get("code")
    if code != 0:
        # 业务错误码：40001 请求为空/缺 text、50001 模型异常等，放行并记录
        logger.warning(
            "[sensitive_service] 检测服务返回业务错误 code=%s，兜底放行", code
        )
        return None

    if "hasSensitiveWord" not in data:
        logger.warning("[sensitive_service] 响应缺少 hasSensitiveWord 字段，兜底放行")
        return None

    return data


async def _run_check(
    text: str,
    stage: str,
    span_name: str,
    do_request,
) -> Dict[str, Any]:
    """两个检测入口的公共骨架：开关/空文本短路 → Langfuse 埋点 → 请求 → 解析。"""
    # 总开关关闭：跳过检测
    if not SENSITIVE_ENABLED:
        return _fallback("client-skip")

    # 服务未配置：跳过检测
    if not SENSITIVE_SERVICE_URL:
        return _fallback("client-skip")

    # 空文本：服务约定返回未命中，直接放行省一次调用
    if not text or not text.strip():
        return _fallback("client-skip")

    langfuse_service = get_current_langfuse()
    span_ctx = (
        langfuse_service.start_span(span_name, input={"text": text, "stage": stage})
        if langfuse_service and langfuse_service.enabled
        else None
    )

    t0 = time.perf_counter()
    result: Dict[str, Any]
    try:
        if span_ctx is None:
            result = await do_request()
        else:
            with span_ctx as span:
                result = await do_request()
                if span:
                    try:
                        span.update(
                            output={
                                "blocked": result["blocked"],
                                "reason": result["reason"],
                                "hit_sources": result["hit_sources"],
                                "sensitive_words": result["sensitive_words"],
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
        # do_request 内部已兜底，此层防御性捕获保证绝不抛出
        logger.warning(
            "[sensitive_service] 检测异常兜底放行 stage=%s: %s", stage, e
        )
        result = _fallback("client-fallback", str(e))
    return result


async def check_sensitive(text: str, stage: str = "output") -> Dict[str, Any]:
    """调用完整敏感检测（词典 + 语义），用于编排输出审核。

    Args:
        text: 待检测文本。为空时直接放行（服务约定空文本未命中）。
        stage: 调用阶段标记（"output" 编排输出检测等），仅用于 Langfuse 埋点与日志。

    Returns:
        {"blocked": bool, "reason": str, "hit_sources": list,
         "sensitive_words": list, "source": str, "raw": dict|None}
        任何异常均返回 blocked=False（兜底放行）。
    """

    async def do_request() -> Dict[str, Any]:
        data = await _post_sensitive(
            f"{_service_base()}/sensitive/check",
            {"text": text, "threshold": SENSITIVE_THRESHOLD},
        )
        if data is None:
            return _fallback("client-fallback")
        return {
            "blocked": bool(data.get("hasSensitiveWord")),
            "reason": str(data.get("reason", "") or ""),
            "hit_sources": list(data.get("hitSources") or []),
            "sensitive_words": list(data.get("sensitiveWords") or []),
            "source": "server",
            "raw": data,
        }

    return await _run_check(text, stage, "sensitive-check", do_request)


async def dict_check_sensitive(text: str, stage: str = "input") -> Dict[str, Any]:
    """调用纯词典敏感检测（/sensitive/dict-check），用于用户输入快速审核。

    与完整检测的差别：不走语义模型，延迟低；请求体仅 {"text": text}；
    响应 {code, hasSensitiveWord, sensitiveWords} 无 reason 字段，
    命中时由客户端构造 reason（列出命中的敏感词）。

    Args:
        text: 待检测文本。为空时直接放行。
        stage: 调用阶段标记（"input" 用户输入检测等），仅用于埋点与日志。

    Returns:
        结构同 check_sensitive；命中时 hit_sources=["dictionary"]。
    """
    async def do_request() -> Dict[str, Any]:
        data = await _post_sensitive(
            f"{_service_base()}/sensitive/dict-check", {"text": text}
        )
        if data is None:
            return _fallback("client-fallback")
        words = list(data.get("sensitiveWords") or [])
        blocked = bool(data.get("hasSensitiveWord"))
        reason = "命中敏感词：" + "、".join(str(w) for w in words) if blocked else ""
        return {
            "blocked": blocked,
            "reason": reason,
            "hit_sources": ["dictionary"] if blocked else [],
            "sensitive_words": words,
            "source": "server",
            "raw": data,
        }

    return await _run_check(text, stage, "sensitive-dict-check", do_request)
