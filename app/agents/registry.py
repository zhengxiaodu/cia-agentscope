"""智能体注册表：加载配置，按智能体绑定的 skill 子集组装独立 Toolkit 并缓存。

设计要点：
- 系统启动时一次性加载所有可用 skill（复用 LocalWorkspace 机制）
- 每个智能体按其 skills 配置，从全量 skill 中筛选出子集，组装独立 Toolkit
- 这样不同智能体只能看到自己绑定的工具，实现职责隔离
"""
import logging
import os
from typing import Dict, List, Optional

import yaml
from agentscope.agent import Agent
from agentscope.model import OpenAIChatModel
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.workspace import LocalWorkspace

from app.agents.base import AgentDefinition

logger = logging.getLogger(__name__)


def load_agent_definitions(config_path: str) -> List[AgentDefinition]:
    """从 agent_config.yml 加载所有智能体定义。"""
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    raw_agents = config.get("agents", [])
    return [AgentDefinition(**a) for a in raw_agents]


async def load_all_skills(skill_config_path: str, workdir: str = "./my-workspace"):
    """加载所有可用 skill，返回 (workspace, all_tools, all_skills_meta)。

    复用 chat_service 中的 LocalWorkspace 加载机制，一次性把所有 skill 装入工作区，
    后续按智能体配置筛选子集。
    """
    with open(skill_config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    skill_loaders = [s["directory"] for s in config.get("skills", [])]

    workspace = LocalWorkspace(
        workdir=workdir,
        default_mcps=[],
        skill_paths=skill_loaders,
    )
    await workspace.initialize()

    all_tools = await workspace.list_tools()
    all_skills_meta = await workspace.list_skills()
    return workspace, all_tools, all_skills_meta


async def load_skills_from_directories(
    directories: list,
    workdir: str = "./my-workspace",
) -> tuple:
    """从目录路径列表加载技能（用于运行时动态追加外部技能）。

    与 load_all_skills 的区别：不依赖 skill_config.yml，
    直接接收 Skill 目录路径列表。

    Args:
        directories: Skill 目录路径列表，如 ["./external_skills/skill_ppt"]
        workdir: LocalWorkspace 工作目录

    Returns:
        (workspace, all_tools, all_skills_meta) 三元组
    """
    workspace = LocalWorkspace(
        workdir=workdir,
        default_mcps=[],
        skill_paths=directories,
    )
    await workspace.initialize()

    all_tools = await workspace.list_tools()
    all_skills_meta = await workspace.list_skills()
    return workspace, all_tools, all_skills_meta


class AgentRegistry:
    """智能体注册表：管理智能体定义、模型实例、按需创建带 skill 子集的 Agent。

    生命周期由 app.main lifespan 管理，单例存于 app.state。
    """

    def __init__(
        self,
        definitions: List[AgentDefinition],
        workspace: LocalWorkspace,
        all_tools: list,
        all_skills_meta: list,
        create_model_fn,
    ):
        """
        Args:
            definitions: 全部智能体定义
            workspace: 已初始化的 LocalWorkspace
            all_tools: 工作区内全部工具
            all_skills_meta: 工作区内全部 skill 元信息
            create_model_fn: 工厂函数，签名 create_model_fn() -> OpenAIChatModel，
                             每次调用返回新的模型实例（流式模型不可复用）
        """
        self._defs: Dict[str, AgentDefinition] = {d.id: d for d in definitions}
        self._workspace = workspace
        self._all_tools = all_tools
        self._all_skills_meta = all_skills_meta
        self._create_model_fn = create_model_fn
        # 缓存每个智能体对应的 Toolkit（按 skill 子集组装），避免重复筛选
        self._toolkits: Dict[str, Toolkit] = {}

    @property
    def definitions(self) -> Dict[str, AgentDefinition]:
        return self._defs

    def get_definition(self, agent_id: str) -> Optional[AgentDefinition]:
        return self._defs.get(agent_id)

    def _build_toolkit_for(self, definition: AgentDefinition) -> Toolkit:
        """根据智能体绑定的 skill 列表，筛选工具子集组装 Toolkit。

        无 skill 的智能体（如 general_agent）返回空 Toolkit，即纯对话无工具。
        """
        if not definition.skills:
            return Toolkit(tools=[], skills_or_loaders=[])

        # 按 skill name 匹配元信息，筛选对应工具
        bound_skill_names = set(definition.skills)

        # 从 skill 元信息中筛选出绑定的 skill loader
        bound_loaders = []
        for meta in self._all_skills_meta:
            # skill 元信息通常含 name 字段
            meta_name = getattr(meta, "name", None) or (
                meta.get("name") if isinstance(meta, dict) else None
            )
            if meta_name in bound_skill_names:
                bound_loaders.append(meta)

        return Toolkit(tools=self._all_tools, skills_or_loaders=bound_loaders)

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

        agent = Agent(
            name=definition.name,
            system_prompt=definition.system_prompt,
            model=model,
            toolkit=toolkit,
            state=agent_state,
        )
        return agent
