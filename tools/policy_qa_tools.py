"""
制度问答（Policy QA）工具

调用制度问答通用接口，将用户问题与知识库 ID 列表传入后端，阻塞返回回答正文与段落级引用文档。

安全性设计：
- 权限名 → 知识库 ID 的映射逻辑在宿主进程内存中（本文件），不注入沙箱，模型不可见。
- 工具签名只暴露 `question` 参数，知识库 ID 列表由宿主侧根据 Redis 中缓存的用户权限
  （agent_whitelist 中的智能体名称）自动映射填充。
- SKILL.md 不含任何具体知识库 ID 或映射关系，模型无法通过阅读技能说明或工具签名获知全部知识库 ID。
"""
import json
import logging
from typing import Optional

import httpx
from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, ToolChunk

from app.config import POLICY_QA_BASE_URL, POLICY_QA_KB_MAP
from app.services.auth_service import get_user_permissions

logger = logging.getLogger(__name__)

# 默认请求超时（秒）：检索 + 模型生成需数秒，留足余量
_DEFAULT_TIMEOUT = 120.0

# 默认权限名 → 知识库 ID 映射
# 可被环境变量 POLICY_QA_KB_MAP（JSON 格式）覆盖
_DEFAULT_PERMISSION_KB_MAP: dict[str, str] = {
    "金科制度问答": "123",
    "信科制度问答": "456",
}


def _load_permission_kb_map() -> dict[str, str]:
    """加载权限名→知识库 ID 映射。

    优先使用环境变量 POLICY_QA_KB_MAP（JSON 格式），
    解析失败或未设置时回退到代码内默认映射 _DEFAULT_PERMISSION_KB_MAP。
    """
    if POLICY_QA_KB_MAP:
        try:
            mapping = json.loads(POLICY_QA_KB_MAP)
            if isinstance(mapping, dict):
                return {str(k): str(v) for k, v in mapping.items()}
            logger.warning(
                "[policy_qa] POLICY_QA_KB_MAP 非 JSON 对象，回退到默认映射"
            )
        except (json.JSONDecodeError, TypeError):
            logger.exception(
                "[policy_qa] POLICY_QA_KB_MAP 解析失败，回退到默认映射"
            )
    return dict(_DEFAULT_PERMISSION_KB_MAP)


def _resolve_kb_ids(permissions: Optional[dict]) -> list[str]:
    """根据用户权限解析可访问的知识库 ID 列表。

    遍历 permissions["agent_whitelist"]，按 name 字段匹配权限名→知识库 ID 映射，
    收集匹配的知识库 ID（去重，保持插入顺序）。

    Args:
        permissions: 用户权限 dict，结构为
            {"agent_whitelist": [{"id","name","code"}, ...], "skill_blacklist": [...]}

    Returns:
        去重后的知识库 ID 列表；无匹配或无权限时返回空列表。
    """
    if not permissions:
        return []
    agent_whitelist = permissions.get("agent_whitelist") or []
    if not isinstance(agent_whitelist, list):
        return []

    kb_map = _load_permission_kb_map()
    kb_ids: list[str] = []
    seen: set[str] = set()
    for item in agent_whitelist:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        kb_id = kb_map.get(name)
        if kb_id and kb_id not in seen:
            seen.add(kb_id)
            kb_ids.append(kb_id)
    return kb_ids


def _format_citations(citations: list) -> str:
    """格式化引用文档列表为可读文本。"""
    if not citations:
        return ""
    lines = []
    for c in citations:
        if not isinstance(c, dict):
            continue
        position = c.get("position", "")
        doc_name = c.get("document_name", "未知文档")
        page_start = c.get("page_start")
        page_end = c.get("page_end")
        page_info = ""
        if page_start is not None and page_end is not None:
            page_info = f" p{page_start}-{page_end}"
        elif page_start is not None:
            page_info = f" p{page_start}"
        dataset_name = c.get("dataset_name", "")
        ds_info = f" [{dataset_name}]" if dataset_name else ""
        lines.append(f"[{position}] {doc_name}{page_info}{ds_info}")
    return "\n".join(lines)


def _build_result(text: str, citations: list = None) -> ToolChunk:
    """构造 ToolChunk 结果。

    citations 写入 metadata，agentscope 会自动透传到
    ToolResultEndEvent.metadata，由 AgentEventTracer 旁路提取。
    """
    metadata = {"citations": citations} if citations else {}
    return ToolChunk(
        content=[TextBlock(text=text)],
        is_last=True,
        metadata=metadata,
    )


def create_policy_qa_tool(user_id: str, redis_client) -> FunctionTool:
    """创建制度问答 FunctionTool（闭包，捕获 user_id 与 redis_client）。

    工具签名只暴露 `question` 参数；知识库 ID 列表由宿主侧根据 Redis 中缓存的
    用户权限自动映射填充，模型无法指定或探测全部知识库 ID。

    Args:
        user_id: 当前用户 ID（来自 JWT）
        redis_client: redis.asyncio 客户端（用于读取用户权限缓存）

    Returns:
        FunctionTool 实例，name="policy_qa"
    """
    async def policy_qa(question: str) -> ToolChunk:
        """调用制度问答 API，基于知识库回答制度/政策/法规类问题。

        知识库 ID 列表由系统根据当前用户权限自动填充，调用方无需（也无法）指定。
        只需传入用户问题文本。

        Args:
            question: 用户的问题文本，至少 1 个字符
        """
        if not question or not question.strip():
            return _build_result("错误：问题不能为空。")

        # 1. 从 Redis 取用户权限
        if not user_id or redis_client is None:
            logger.warning("[policy_qa] 缺少 user_id 或 redis_client，无法获取权限")
            return _build_result("您没有制度问答权限，请联系管理员开通。")

        try:
            perms_data = await get_user_permissions(redis_client, user_id)
        except Exception:
            logger.exception(f"[policy_qa] 读取用户权限失败 user={user_id}")
            return _build_result("您没有制度问答权限，请联系管理员开通。")

        if not perms_data:
            return _build_result("您没有制度问答权限，请联系管理员开通。")

        permissions = perms_data.get("permissions") or {}

        # 2. 解析可访问的知识库 ID
        kb_ids = _resolve_kb_ids(permissions)
        if not kb_ids:
            return _build_result("您没有制度问答权限，请联系管理员开通。")

        # 3. 调用制度问答后端接口
        base = POLICY_QA_BASE_URL.rstrip("/")
        url = f"{base}/api/v1/policy/qa"
        payload = {
            "question": question,
            "knowledge_base_ids": kb_ids,
        }

        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            if resp.status_code != 200:
                return _build_result(
                    f"制度问答接口返回错误: HTTP {resp.status_code} - {resp.text}"
                )

            data = resp.json()
            answer = data.get("answer", "")
            citations = data.get("citations") or []

            if not answer:
                return _build_result("未检索到相关内容，无法回答。")

            # 4. 格式化输出：回答正文 + 引用来源
            content = answer
            citations_text = _format_citations(citations)
            if citations_text:
                content += "\n\n--- 引用来源 ---\n" + citations_text

            # citations 写入 ToolChunk.metadata，由框架透传到事件层
            return _build_result(content, citations=citations)

        except httpx.TimeoutException:
            logger.exception(f"[policy_qa] 请求超时 url={url}")
            return _build_result("制度问答请求超时，请稍后重试。")
        except httpx.HTTPError as e:
            logger.exception(f"[policy_qa] 网络异常 url={url}")
            return _build_result(f"制度问答网络异常: {e}")
        except Exception as e:
            logger.exception(f"[policy_qa] 调用异常 user={user_id}")
            return _build_result(f"制度问答调用异常: {e}")

    return FunctionTool(
        func=policy_qa,
        name="policy_qa",
        description=(
            "调用制度问答 API，基于知识库回答制度/政策/法规类问题。"
            "当用户提出需要从指定知识库中检索并回答的制度、政策、规范类问题时调用此技能。"
            "只需传入用户问题，系统会自动根据用户权限检索对应知识库。"
        ),
    )
