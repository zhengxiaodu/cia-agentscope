"""
博查网络搜索（Bocha Web Search）工具

宿主侧调用博查 Web Search API（POST /v1/web-search），返回格式化搜索结果
文本与来源摘要（bocha_sum）。bocha_sum 写入 ToolChunk.metadata，由
AgentEventTracer 旁路提取，经 bocha_sum 事件流转发前端，并随 assistant
消息落库（messages.bocha_sum）。

安全性设计：
- API key 只存在于宿主进程环境变量（BOCHA_API_KEY），不注入沙箱，模型不可见。
- 工具签名只暴露 `query` 参数，其余请求参数使用博查默认值。

引用编号机制（请求级统一来源编号 citation_id）：
- create_bocha_search_tool 闭包内维护请求级来源表（sources + url_to_id）：
  同一轮请求内多次搜索、多智能体并发搜索共享一套连续编号，正文 {n} 引用
  与最终来源列表按 citation_id 对齐；下游用 merge_bocha_snapshots 按
  citation_id 幂等合并各次搜索返回的 bocha_sum 快照。
- 来源表是闭包状态，随工具实例生命周期存续：工厂每请求调用一次，下一轮
  请求创建新实例、编号从 1 重新开始；禁止将工具实例做成模块级单例或跨
  请求缓存复用，否则来源表会跨请求/跨用户共享。
- URL 仅 strip 后精确比较，不做尾斜杠/参数/scheme 等任何归一化；strip 后
  为空的 URL 直接过滤，不进来源表、不占编号、不进正文与快照。
- 搜索失败（超时/网络异常/HTTP 错误/解析失败/业务错误码/无有效结果）只
  返回友好文案，metadata 为空，不占用编号。
- 来源条目首次登记后字段不可变（重复 URL 沿用首次登记的条目与编号），
  metadata 携带的 bocha_sum 是来源表此时的复制快照，不随后续登记变化。
- 编号从 1 连续递增、无跳号，"数组下标 + 1 == citation_id"恒成立。
"""
import logging
from typing import List, Optional

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

# bocha_sum 条目的统一来源编号字段名（请求级连续编号，从 1 开始）
_CITATION_ID_FIELD = "citation_id"


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
    """格式化搜索结果为可读文本（供模型归纳引用）。

    展示编号取条目的 citation_id（本轮多次搜索共用的统一编号），
    不按本次结果局部重新编号。
    """
    lines = []
    for s in bocha_sum:
        cid = s[_CITATION_ID_FIELD]
        lines.append(f"[{cid}] {s['name']}")
        summary = s.get("summary") or s.get("snippet") or ""
        if summary:
            lines.append(summary)
        lines.append(f"链接: {s['url']}")
    lines.append(
        "\n请在回答正文中用 {n} 序号标注引用来源（n 对应上述 [n] 编号），"
        "不要在正文中罗列完整链接。上述编号是本轮多次搜索共用的统一编号："
        "直接使用所示编号，不要自行重新编号，也不要沿用历史轮次的编号。"
    )
    return "\n".join(lines)


def merge_bocha_snapshots(existing, incoming) -> List[dict]:
    """按 citation_id 幂等合并 bocha_sum 快照，按编号升序输出。

    供编排层（base.py / orchestrator_service.py 的 bocha_emitted）与统一出口
    （chat_service.py 的本轮总表）复用，取代直接 extend：
    - 相同 citation_id 仅保留首次出现的条目（existing 优先；来源条目登记后
      不可变，二者内容本应一致）；
    - 缺失 citation_id 的条目原样保留在输出尾部（防御性兜底，正常事件流中
      不出现——工具输出的条目均含 citation_id）；
    - 非 dict 条目跳过。
    前提：编号只增不减，调用方可用"合并后长度增长"判定出现新编号。
    """
    by_id: dict = {}
    tail: list = []
    for snapshot in (existing, incoming):
        for s in snapshot:
            if not isinstance(s, dict):
                continue
            cid = s.get(_CITATION_ID_FIELD)
            if cid is None:
                if s not in tail:  # 完全相同的 dict 去重
                    tail.append(s)
            elif cid not in by_id:  # existing 先处理，同 ID 保留首次出现
                by_id[cid] = s
    return [by_id[cid] for cid in sorted(by_id)] + tail


def create_bocha_search_tool() -> FunctionTool:
    """创建博查网络搜索 FunctionTool。

    无闭包参数：API 地址与 key 从宿主进程环境变量读取。
    受请求级 search_enabled 开关控制是否注入（见 orchestrator_service）。

    闭包内维护请求级来源表（sources + url_to_id）：本工厂每请求调用一次
    （见 workspace_assembler），同轮所有智能体共享同一套 citation_id 编号；
    下一轮请求创建新实例、编号从 1 重新开始。禁止将工具实例做成模块级
    单例或跨请求缓存复用，否则来源表会跨请求/跨用户共享。
    """
    # 请求级来源表。来源条目首次登记后字段不可变——这是下游
    # "仅出现新 citation_id 才发送"变更门控的前提，后续禁止更新条目。
    sources: list = []
    url_to_id: dict = {}

    async def bocha_web_search(query: str = "") -> ToolChunk:
        """调用博查搜索API，搜索互联网上的最新信息和实时数据。

        当用户询问最新消息、实时信息（新闻、天气、股价等）或需要
        网络检索支持时调用此工具。

        Args:
            query: 搜索关键词，简洁明了，至少 1 个字符
        """
        # query 设默认值而非必填：私有推理引擎偶发首调用空参 {}，
        # 必填签名会在框架参数校验层抛 TypeError（ERROR 态事件直发前端，
        # 用户可见"执行失败 {}"）。改为默认空串后空参调用落到此处，
        # 以正常状态返回提示文案，模型读后自行补参重试，前端无感。
        if not query or not query.strip():
            return _build_result(
                "搜索关键词为空。请携带明确的搜索关键词（query 参数）"
                "重新调用本工具。"
            )

        if not BOCHA_API_KEY:
            return _build_result("搜索服务未配置，请联系管理员。")

        url = BOCHA_API_BASE.rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {BOCHA_API_KEY}",
        }
        payload = {"query": query.strip(), "summary": True, "count": 10}

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
        # ---- 同步步骤（不含 await，asyncio 单线程下天然互斥）：
        # 过滤 → 登记/编号 → 复制快照 ----
        # 空/缺 URL（strip 后为空）不进来源表、不占编号、不进正文与快照
        valid: list = []
        for s in bocha_sum:
            url = (s.get("url") or "").strip()
            if not url:
                continue
            valid.append({**s, "url": url})
        if not valid:
            return _build_result("未搜索到相关结果。")
        # 新 URL 分配连续编号；重复 URL 沿用原编号且保留首次登记信息
        current_items: list = []
        for s in valid:
            url = s["url"]
            cid = url_to_id.get(url)
            if cid is None:
                cid = len(sources) + 1
                s[_CITATION_ID_FIELD] = cid
                sources.append(s)  # 新建 dict，登记后不再修改
                url_to_id[url] = cid
            else:
                s = sources[cid - 1]  # 编号连续无跳号，下标 = 编号-1
            current_items.append(s)
        # 复制快照（列表 + 逐字典复制；字段均为不可变字符串，无递归深拷贝），
        # 避免早先返回的 metadata 随后修改
        snapshot = [dict(s) for s in sources]
        return _build_result(
            _format_search_results(current_items), bocha_sum=snapshot,
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
