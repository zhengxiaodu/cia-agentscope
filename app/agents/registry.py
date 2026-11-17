"""智能体注册表：加载配置，按智能体绑定的 skill 子集组装独立 Toolkit 并缓存。

设计要点：
- 系统启动时一次性加载所有可用 skill（按工作区机制装载）
- 每个智能体按其 skills 配置，从全量 skill 中筛选出子集，组装独立 Toolkit
- 这样不同智能体只能看到自己绑定的工具，实现职责隔离
"""
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

import yaml
from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.model import OpenAIChatModel
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from app.agents.base import AgentDefinition
from app.intent.rewriter import _get_current_date_str
from app.utils.task_completion_guard import TASK_COMPLETION_GUIDANCE, TaskCompletionGuard

logger = logging.getLogger(__name__)

def load_agent_definitions(config_path: str) -> List[AgentDefinition]:
    """从 agent_config.yml 加载所有智能体定义。"""
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    raw_agents = config.get("agents", [])
    return [AgentDefinition(**a) for a in raw_agents]


class AgentRegistry:
    """智能体注册表：管理智能体定义、模型实例、按需创建带 skill 子集的 Agent。

    生命周期由 app.main lifespan 管理，单例存于 app.state。
    """

    def __init__(
        self,
        definitions: List[AgentDefinition],
        workspace: Any,
        all_tools: list,
        all_skills_meta: list,
        create_model_fn,
        extra_skill_names: Optional[List[str]] = None,
        agent_bound_tools: Optional[dict] = None,
    ):
        """
        Args:
            definitions: 全部智能体定义
            workspace: 已初始化的工作区实例
            all_tools: 工作区内全部工具
            all_skills_meta: 工作区内全部 skill 元信息
            create_model_fn: 工厂函数，签名 create_model_fn() -> OpenAIChatModel，
                             每次调用返回新的模型实例（流式模型不可复用）
            extra_skill_names: 请求级附加技能名，union 到每个 agent 的绑定技能
                               （无论该 agent 是否绑定该技能，都会组装进其 Toolkit）
            agent_bound_tools: 工具智能体绑定（工具名 → 智能体 id 列表），
                               声明的工具只注入给列表内智能体，其余智能体不可见
        """
        self._defs: Dict[str, AgentDefinition] = {d.id: d for d in definitions}
        self._workspace = workspace
        self._all_tools = all_tools
        self._all_skills_meta = all_skills_meta
        self._create_model_fn = create_model_fn
        # 请求级附加技能：组装 Toolkit 时与 agent 自身绑定技能取并集
        self._extra_skill_names = set(extra_skill_names or [])
        # 工具智能体绑定：声明的工具仅注入给指定智能体
        self._agent_bound_tools: dict = {
            str(k): list(v or []) for k, v in (agent_bound_tools or {}).items()
        }
        # 缓存每个智能体对应的 Toolkit（按 skill 子集组装），避免重复筛选
        self._toolkits: Dict[str, Toolkit] = {}

    @property
    def definitions(self) -> Dict[str, AgentDefinition]:
        return self._defs

    def get_definition(self, agent_id: str) -> Optional[AgentDefinition]:
        return self._defs.get(agent_id)

    def _build_mcp_clients(self, definition: AgentDefinition) -> list:
        """根据智能体定义中的 mcp_client_config 构建 stateless HTTP MCP 客户端。

        配置来自登录时 mng 下发的 agent_access 项 mcpClientConfig
        （经 merge_external_into_memory 附加、Redis 缓存透传）。
        字段缺失/构建失败时记日志返回空列表，不影响该智能体其他工具；
        MCP 服务不可达由 Toolkit 在列举工具时自动跳过，不影响对话。

        Returns:
            [MCPClient, ...]，最多一个（每个白名单项至多一个 mcpClientConfig）
        """
        cfg = getattr(definition, "mcp_client_config", None)
        if not isinstance(cfg, dict) or not cfg:
            return []

        transport_url = str(cfg.get("transport_url") or "")
        if not transport_url:
            logger.warning(
                f"[AgentRegistry] agent={definition.id} "
                f"mcpClientConfig 缺 transport_url，跳过 MCP 注册"
            )
            return []

        # query_params：dict 用 urlencode 拼接；str 视为已编码的查询串
        query_params = cfg.get("query_params")
        if isinstance(query_params, dict) and query_params:
            sep = "&" if "?" in transport_url else "?"
            transport_url = f"{transport_url}{sep}{urlencode(query_params)}"
        elif isinstance(query_params, str) and query_params.strip():
            sep = "&" if "?" in transport_url else "?"
            transport_url = f"{transport_url}{sep}{query_params.lstrip('?')}"

        # transport_type 仅用于校验告警：AgentScope 按 URL path 自动判定
        # SSE（/sse、/messages/ 结尾）与 Streamable HTTP，不改造 URL
        transport_type = str(cfg.get("transport_type") or "").lower()
        if transport_type == "sse":
            path = urlsplit(transport_url).path
            if not (path.endswith("/sse") or path.endswith("/messages/")):
                logger.warning(
                    f"[AgentRegistry] agent={definition.id} transport_type=sse "
                    f"但 URL 不符合 SSE 路径模式，按 URL 自动判定: {transport_url}"
                )

        # authorization 为完整 Authorization 请求头值，原样使用
        headers = {}
        authorization = cfg.get("authorization")
        if authorization:
            headers["Authorization"] = authorization

        # MCPClient name 须匹配 ^[a-zA-Z0-9_-]+$（组合工具名 mcp__{name}__tool）
        name = re.sub(
            r"[^a-zA-Z0-9_-]", "_", str(cfg.get("code") or definition.id)
        ) or "mcp_server"

        try:
            from agentscope.mcp import MCPClient, HttpMCPConfig

            return [
                MCPClient(
                    name=name,
                    # stateless：无需 connect/close 生命周期，不可达自动跳过
                    is_stateful=False,
                    mcp_config=HttpMCPConfig(
                        url=transport_url,
                        headers=headers or None,
                    ),
                )
            ]
        except Exception:
            logger.exception(
                f"[AgentRegistry] agent={definition.id} MCP 客户端构建失败，跳过"
            )
            return []

    def _filter_tools_for_agent(self, definition: AgentDefinition, tools: list) -> list:
        """工具绑定过滤：agent_bound_tools 声明的工具仅注入给列表内智能体。

        未声明的工具保持全局可见（行为与无配置时一致）。
        """
        if not self._agent_bound_tools:
            return tools
        result = []
        for t in tools:
            name = getattr(t, "name", None)
            if name in self._agent_bound_tools:
                allowed = self._agent_bound_tools.get(name) or []
                if definition.id not in allowed:
                    continue
            result.append(t)
        return result

    def _build_toolkit_for(self, definition: AgentDefinition) -> Toolkit:
        """根据智能体绑定的 skill 列表，筛选工具子集组装 Toolkit。

        有效绑定技能 = agent 自身绑定技能 ∪ 请求级附加技能（extra_skill_names）。
        无任何绑定技能时返回空 Toolkit，即纯对话无工具。
        工具侧：agent_bound_tools 声明的绑定工具仅注入给列表内智能体。
        """
        # MCP 客户端（agent_access 项 mcpClientConfig → stateless HTTP MCP）
        mcps = self._build_mcp_clients(definition)

        # 有效绑定技能 = agent 自身绑定 ∪ 请求级附加技能
        bound_skill_names = set(definition.skills) | self._extra_skill_names
        if not bound_skill_names:
            return Toolkit(tools=[], skills_or_loaders=[], mcps=mcps)

        # 工具绑定过滤：非绑定智能体拿不到声明的工具
        tools = self._filter_tools_for_agent(definition, self._all_tools)

        # 从 skill 元信息中筛选出绑定的 skill loader
        bound_loaders = []
        matched_names = set()
        for meta in self._all_skills_meta:
            # skill 元信息通常含 name 字段
            meta_name = getattr(meta, "name", None) or (
                meta.get("name") if isinstance(meta, dict) else None
            )
            if meta_name in bound_skill_names:
                bound_loaders.append(meta)
                matched_names.add(meta_name)

        # 请求了但 workspace 未装载的技能 → 记 warning 跳过
        missing = bound_skill_names - matched_names
        if missing:
            logger.warning(
                f"[AgentRegistry] 技能未在 workspace 装载，已跳过: {sorted(missing)}"
            )

        return Toolkit(
            tools=tools, skills_or_loaders=bound_loaders, mcps=mcps
        )

    def get_toolkit(self, agent_id: str) -> Optional[Toolkit]:
        """获取智能体的 Toolkit（带缓存）。"""
        definition = self._defs.get(agent_id)
        if definition is None:
            return None
        if agent_id not in self._toolkits:
            self._toolkits[agent_id] = self._build_toolkit_for(definition)
        return self._toolkits[agent_id]

    def create_agent(
        self,
        agent_id: str,
        session_id: Optional[str] = None,
        agent_state: Optional[AgentState] = None,
    ) -> Optional[Agent]:
        """创建一个 Agent 实例。

        Args:
            agent_id: 智能体 id
            session_id: 会话 id（用于 AgentState）
            agent_state: 已恢复的 AgentState（多轮上下文），优先使用；为 None 则新建

        Returns:
            Agent 实例，agent_id 不存在返回 None
        """
        definition = self._defs.get(agent_id)
        if definition is None:
            logger.warning(f"[AgentRegistry] 未知智能体: {agent_id}")
            return None

        # 每次创建新的模型实例（OpenAIChatModel 流式会话不可跨调用复用）
        model = self._create_model_fn()
        toolkit = self.get_toolkit(agent_id)

        # 状态：优先用传入的已恢复状态，否则新建
        if agent_state is None:
            agent_state = AgentState(
                session_id=session_id,
                permission_context=PermissionContext(mode=PermissionMode.BYPASS),
            )
        react_config = ReActConfig()
        react_config.max_iters = 200

        # 联网搜索可用性按本轮实际注册的工具判定（all_tools 由
        # workspace_assembler 按 search_enabled 注入），提示词与函数
        # 列表保持一致，避免模型被"提示词说有、函数列表说没有"的
        # 矛盾信息驱动去沙箱目录翻找搜索技能
        has_web_search = any(
            getattr(t, "name", None) == "bocha_web_search"
            for t in self._all_tools
        )
        if has_web_search:
            external_info_guidance = (
                "\n你仅允许通过系统内置的技能/工具获取外部信息（如联网搜索、"
                "知识库查询等），禁止自行使用curl、wget、requests或其他方式"
                "直接访问外部接口；禁止下载、安装额外的依赖包。若现有信息"
                "不足以解答用户问题，请如实告知、或尝试挖掘已有线索进行处理。"
            )
        else:
            external_info_guidance = (
                "\n你仅允许通过系统内置的技能/工具获取外部信息（如知识库查询等），"
                "禁止自行使用curl、wget、requests或其他方式直接访问外部接口；"
                "禁止下载、安装额外的依赖包。本轮未开启联网搜索：涉及实时信息"
                "（新闻、天气、股价等）时，请直接告知用户当前无法联网检索并"
                "基于已有知识作答（注明可能非最新）。禁止在沙箱目录中查找、"
                "阅读或尝试执行搜索技能与脚本。若现有信息不足以解答用户问题，"
                "请如实告知、或尝试挖掘已有线索进行处理。"
            )

        # 本智能体无绑定技能时不下发技能目录使用说明，避免引导模型
        # 翻找 /workspace/skills/（目录中物理存在但本轮未启用的技能文件）
        bound_skill_names = set(definition.skills) | self._extra_skill_names
        skill_dir_guidance = (
            "\n当你调用技能时，必须在/workspace/skills目录下找到技能的说明文件"
            "SKILL.md并阅读说明，阅读完成后才能继续执行。注意：只有函数列表中"
            "已注册对应工具的技能才可使用；目录中存在但函数列表无对应工具的"
            "技能（如本轮未开启的联网搜索）一律不可用，禁止翻找或执行其脚本。"
            if bound_skill_names
            else ""
        )

        history_guidance = ""
        if (definition.skills or self._extra_skill_names) and any(
            getattr(t, "name", None) == "RestoreHistoryFile"
            for t in self._filter_tools_for_agent(definition, self._all_tools)
        ):
            history_guidance = (
                "\n继续修改本会话以前生成的 PPTX、文档、表格或图片时，先用 ListHistoryFiles"
                "查询历史文件清单，用精确 path 调用 RestoreHistoryFile，把二进制原件按需加载到"
                "当前工作区，再使用返回的绝对路径读取或修改。工作区暂未加载旧文件不代表文件已丢失。"
                "同名文件先按路径和消息配对标识确认；工具报错时如实说明。修改结果另存新文件名，保留旧文件。"
            )

        agent = Agent(
            name=definition.name,
            system_prompt=definition.system_prompt
            + f"\n当前时间：{_get_current_date_str()}。涉及\"今天/本周/最近\"等时间表述时以此为准。"
            + f"\n注意当前工作区目录是/data/workspaces/{session_id}，你只能在该目录下创建/编辑文件，绝不允许修改目录以外的文件！"
            + "\n用户明确要求不生成文档或附件时，只返回聊天正文，可以使用 Markdown 排版，不创建或导出 .md、.docx 等附件。"
            + "\n用户本轮上传的文件内容会直接附在用户问题里，请直接阅读使用。此外，本会话此前轮次上传文件的解析副本存放在工作目录 uploads/ 下（文件名为 <原文件名>.md）：当用户点名或指代历史文件时，可读取 uploads/ 下对应文件获取内容。"
            + "\n当你生成文件时，生成的文件内容只要用户不做特殊说明应该以中文为主。"
            + "\n当用户要求修改文件时，必须生成新文件名（如添加版本号、时间戳或_modified后缀），严禁直接覆盖原文件，以防止数据丢失。"
            + external_info_guidance
            + skill_dir_guidance
            + history_guidance
            + TASK_COMPLETION_GUIDANCE,
            model=model,
            toolkit=toolkit,
            middlewares=[TaskCompletionGuard()],
            state=agent_state,
            context_config=ContextConfig(
                trigger_ratio=0.7,
                reserve_ratio=0.4,
                tool_result_limit=3000,
                # 压缩（总结）失败时回退到直接截断旧上下文，而不是抛错
                # 中断对话（agentscope 默认即为 True，这里显式声明）
                compression_fallback_to_truncation=True,
            ),
            react_config=react_config
        )
        # 压缩防护：私有引擎对结构化输出的 grammar 编译 bug 会导致
        # 自动压缩抛 APIError（2.0.3 无降级梯子，异常直接炸掉整个会话）。
        # 包装为软失败：压缩失败仅跳过本轮压缩，对话继续。
        _orig_compress = agent.compress_context

        async def _safe_compress_context(*args, **kwargs):
            try:
                return await _orig_compress(*args, **kwargs)
            except Exception as e:
                logger.warning("[AgentRegistry] 上下文压缩失败，已跳过: %s", e)
                return None

        agent.compress_context = _safe_compress_context
        return agent
