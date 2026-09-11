"""
博查网络搜索（Bocha Web Search）工具

宿主侧调用博查 Web Search API（POST /v1/web-search），返回格式化搜索结果
文本与来源摘要（bocha_sum）。bocha_sum 写入 ToolChunk.metadata，由
AgentEventTracer 旁路提取，经 bocha_sum 事件流转发前端，并随 assistant
消息落库（messages.bocha_sum）。

安全性设计：
- API key 只存在于宿主进程环境变量（BOCHA_API_KEY），不注入沙箱，模型不可见。
- 工具签名只暴露 `query` 参数，其余请求参数使用博查默认值。
"""
import logging
from typing import Optional

import httpx
from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, ToolChunk

from app.config import BOCHA_API_BASE, BOCHA_API_KEY

logger = logging.getLogger(__name__)

# 工具内部请求超时（秒），不进配置
_BOCHA_TIMEOUT = 15

# 每条来源进入 bocha_sum 的字段（博查 WebPageValue 子集）
_BOCHA_SUM_FIELDS = (
    "name", "url", "snippet", "summary", "siteName", "dateLastCrawled",
)


def _build_result(text: str, bocha_sum: Optional[list] = None) -> ToolChunk:
    """构造 ToolChunk 结果。

    bocha_sum 写入 metadata，agentscope 会自动透传到
    ToolResultEndEvent.metadata，由 AgentEventTracer 旁路提取。
    """
    metadata = {"bocha_sum": bocha_sum} if bocha_sum else {}
    return ToolChunk(
        content=[TextBlock(text=text)],
        is_last=True,
        metadata=metadata,
    )


def _extract_bocha_sum(web_pages: list) -> list:
    """从博查 webPages.value 提取来源摘要字段子集。"""
    result = []
    for p in web_pages:
        if not isinstance(p, dict):
            continue
        result.append({k: p.get(k, "") or "" for k in _BOCHA_SUM_FIELDS})
    return result


def _format_search_results(bocha_sum: list) -> str:
    """格式化搜索结果为可读文本（供模型归纳引用）。"""
    lines = []
    for i, s in enumerate(bocha_sum, start=1):
        lines.append(f"[{i}] {s['name']}")
        summary = s.get("summary") or s.get("snippet") or ""
        if summary:
            lines.append(summary)
        lines.append(f"链接: {s['url']}")
    return "\n".join(lines)


def create_bocha_search_tool() -> FunctionTool:
    """创建博查网络搜索 FunctionTool。

    无闭包参数：API 地址与 key 从宿主进程环境变量读取。
    受请求级 search_enabled 开关控制是否注入（见 orchestrator_service）。

    Returns:
        FunctionTool 实例，name="bocha_web_search"
    """
    async def bocha_web_search(query: str) -> ToolChunk:
        """调用博查搜索API，搜索互联网上的最新信息和实时数据。

        当用户询问最新消息、实时信息（新闻、天气、股价等）或需要
        网络检索支持时调用此工具。

        Args:
            query: 搜索关键词，简洁明了，至少 1 个字符
        """
        if not query or not query.strip():
            return _build_result("错误：搜索关键词不能为空。")

        if not BOCHA_API_KEY:
            return _build_result("搜索服务未配置，请联系管理员。")

        url = f"{BOCHA_API_BASE.rstrip('/')}/v1/web-search"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {BOCHA_API_KEY}",
        }
        payload = {"query": query.strip()}

        try:
            async with httpx.AsyncClient(timeout=_BOCHA_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            logger.warning("[bocha_search] 搜索请求超时（%ss）", _BOCHA_TIMEOUT)
            return _build_result("搜索请求超时，请稍后重试。")
        except Exception as e:
            logger.warning("[bocha_search] 搜索网络异常: %s", e)
            return _build_result(f"搜索网络异常: {e}")

        if resp.status_code != 200:
            logger.warning(
                "[bocha_search] 接口返回错误: HTTP %s - %s",
                resp.status_code, resp.text[:200],
            )
            return _build_result(
                f"搜索接口返回错误: HTTP {resp.status_code}"
            )

        try:
            data = resp.json()
        except Exception:
            logger.warning("[bocha_search] 响应解析失败")
            return _build_result("搜索响应解析失败，请稍后重试。")

        if not isinstance(data, dict) or data.get("code") not in (200, None):
            logger.warning("[bocha_search] 业务错误码: %s", data.get("code"))
            return _build_result("搜索服务返回错误，请稍后重试。")

        web_pages = (
            (data.get("data") or {}).get("webPages") or {}
        ).get("value") or []
        if not web_pages:
            return _build_result("未搜索到相关结果。")

        bocha_sum = _extract_bocha_sum(web_pages)
        return _build_result(
            _format_search_results(bocha_sum), bocha_sum=bocha_sum,
        )

    return FunctionTool(
        func=bocha_web_search,
        name="bocha_web_search",
        description=(
            "调用博查搜索API，搜索互联网上的最新信息和实时数据。"
            "当用户询问最新消息、实时信息（新闻、天气、股价、体育赛事等）"
            "或需要网络检索支持时调用此工具。"
        ),
    )
