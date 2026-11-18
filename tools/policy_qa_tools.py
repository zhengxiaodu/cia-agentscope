"""
制度问答（Policy QA）工具

进程内调用本地制度问答服务（app.regulations.services.PolicyQAService），将用户问题
与知识库 ID 列表传入，阻塞返回回答正文与段落级引用文档（不再远程调用独立后端）。

安全性设计：
- 权限名 → 知识库 ID 的映射逻辑在宿主进程内存中（本文件），不注入沙箱，模型不可见。
- 工具签名只暴露 `question` 参数，知识库 ID 列表由宿主侧根据 Redis 中缓存的用户权限
  （agent_whitelist 中的智能体名称）自动映射填充。
- SKILL.md 不含任何具体知识库 ID 或映射关系，模型无法通过阅读技能说明或工具签名获知全部知识库 ID。
"""
import json
import logging
from typing import Optional

from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, ToolChunk

from app.config import POLICY_QA_KB_MAP
from app.services.auth_service import get_user_permissions

logger = logging.getLogger(__name__)

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


def _resolve_kb_entries(permissions: Optional[dict]) -> list[tuple[str, str]]:
    """根据用户权限解析可访问的 (权限名, 知识库 ID) 列表。

    遍历 permissions["agent_whitelist"]，按 name 字段匹配权限名→知识库 ID 映射，
    收集匹配项（kb_id 去重，保持插入顺序）。

    Args:
        permissions: 用户权限 dict，结构为
            {"agent_whitelist": [{"id","name","code"}, ...], "skill_blacklist": [...]}

    Returns:
        [(权限名, kb_id), ...]；无匹配或无权限时返回空列表。
    """
    if not permissions:
        return []
    agent_whitelist = permissions.get("agent_whitelist") or []
    if not isinstance(agent_whitelist, list):
        return []

    kb_map = _load_permission_kb_map()
    entries: list[tuple[str, str]] = []
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
            entries.append((name, kb_id))
    return entries


def _resolve_kb_ids(permissions: Optional[dict]) -> list[str]:
    """根据用户权限解析可访问的知识库 ID 列表（语义与历史版本保持一致）。"""
    return [kb_id for _, kb_id in _resolve_kb_entries(permissions)]


def resolve_kb_ids(
    permissions: Optional[dict],
    knowledge_bases: Optional[list[str]] = None,
) -> list[str]:
    """权限解析 + 知识库选择过滤的公共入口（问答工具与文档路由共用）。

    Args:
        permissions: 用户权限 dict
        knowledge_bases: 前端所选知识库名列表；None/空 = 全选（不过滤）。
            非空时与权限映射取交集，无效项静默忽略；交集可能为空。

    Returns:
        去重后的知识库 ID 列表；全选语义下无匹配返回空列表。
    """
    entries = _resolve_kb_entries(permissions)
    if knowledge_bases:
        selected = {str(n) for n in knowledge_bases}
        entries = [(n, k) for n, k in entries if n in selected]
    return [kb_id for _, kb_id in entries]


def resolve_user_kb_names(permissions: Optional[dict]) -> list[str]:
    """解析用户可访问的知识库名列表（登录响应下发前端渲染选择器用）。"""
    return [name for name, _ in _resolve_kb_entries(permissions)]


def _normalize_citation_markers(answer: str, resource_count: int) -> str:
    """把 answer 中的 [n] 引用标记统一转换为 {n}（n 在 1..resource_count 范围内）。

    兼容模型输出方括号编号的情况，确保前端能按 {n} 渲染引用角标。
    不影响已有的 {n} 标记，也不转换越界编号。
    """
    if not answer or resource_count <= 0:
        return answer
    import re

    def _repl(m: re.Match) -> str:
        n = int(m.group(1))
        return f"{{{n}}}" if 1 <= n <= resource_count else m.group(0)

    return re.sub(r"\[(\d+)\]", _repl, answer)


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


def create_policy_qa_tool(
    user_id: str,
    redis_client,
    knowledge_bases: Optional[list[str]] = None,
) -> FunctionTool:
    """创建制度问答 FunctionTool（闭包，捕获 user_id 与 redis_client）。

    工具签名只暴露 `question` 参数；知识库 ID 列表由宿主侧根据 Redis 中缓存的
    用户权限自动映射填充，模型无法指定或探测全部知识库 ID。

    Args:
        user_id: 当前用户 ID（来自 JWT）
        redis_client: redis.asyncio 客户端（用于读取用户权限缓存）
        knowledge_bases: 前端所选知识库名列表（请求级）；None/空 = 全选
            （用户权限内全部知识库），非空时只查所选库。无效选择静默忽略，
            所选全部不在权限内时返回引导重新登录的提示。

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

        # 2. 解析可访问的知识库 ID（结合前端知识库选择过滤）
        kb_ids = resolve_kb_ids(permissions, knowledge_bases)
        if not kb_ids:
            if knowledge_bases:
                # 用户做了选择但与当前权限无交集：权限快照可能已过期
                #（重新登录后被覆盖），引导重新登录刷新
                return _build_result(
                    "您没有所选知识库的访问权限，请退出重新登录后重试。"
                )
            return _build_result("您没有制度问答权限，请联系管理员开通。")

        logger.info(
            f"[policy_qa] knowledge_bases={knowledge_bases} -> kb_ids={kb_ids}"
        )

        # 3. 进程内调用本地制度问答服务
        from app.regulations.runtime import get_policy_qa_service

        try:
            svc = get_policy_qa_service()
        except RuntimeError:
            logger.warning("[policy_qa] 制度问答服务未初始化")
            return _build_result("制度问答服务暂不可用，请稍后重试。")

        try:
            result = await svc.run_general_qa(
                question=question, kb_ids=kb_ids, user_id=user_id, user_department="",
            )
            answer = result.answer
            citations = [c.model_dump() for c in result.citations]

            if not answer:
                return _build_result("未检索到相关内容，无法回答。")

            # 保险：把 answer 里的 [n]（n 在 1..引用数范围内）统一转成 {n}，
            # 兼容模型输出方括号编号的情况，确保前端能渲染引用角标
            answer = _normalize_citation_markers(answer, len(citations))

            # 4. 格式化输出：用明确标记包裹 answer，告诉 agent 直接使用、
            #    不要改写（避免 agent 重新组织语言时丢失 {n} 引用标记）
            content = "【制度问答回答】\n" + answer
            citations_text = _format_citations(citations)
            if citations_text:
                content += (
                    "\n\n--- 引用来源 ---\n" + citations_text
                    + "\n\n请直接输出【制度问答回答】部分的内容作为最终回答，"
                    "不要改写、不要重新组织语言。回答中的 {n} 是引用标记，"
                    "必须原样保留，对应上述引用来源的 [n] 编号。"
                )

            # citations 写入 ToolChunk.metadata，由框架透传到事件层
            return _build_result(content, citations=citations)

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
