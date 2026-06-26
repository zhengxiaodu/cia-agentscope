"""智能体工厂：意图 → 智能体实例。

根据意图识别结果中的 agent_id（由 intent_config.yml 的 intent.agent 映射），
从 AgentRegistry 取得对应智能体并创建实例。

注：不直接依赖 Intent 数据类型（避免与 app.intent 循环依赖），
仅按 agent_id 字符串路由，由上层编排器传入。
"""
import logging
from typing import Optional

from agentscope.agent import Agent
from agentscope.state import AgentState

from app.agents.registry import AgentRegistry

logger = logging.getLogger(__name__)


class AgentFactory:
    """意图→智能体的工厂门面，封装 AgentRegistry 的创建逻辑。"""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    def create_for_agent(
        self,
        agent_id: str,
        session_id: Optional[str] = None,
        agent_state: Optional[AgentState] = None,
    ) -> Optional[Agent]:
        """根据 agent_id 创建智能体实例。

        Args:
            agent_id: 智能体标识（来自 intent_config 中 intent.agent 的映射）
            session_id: 会话 id
            agent_state: 已恢复的状态（多轮上下文）

        Returns:
            Agent 实例；agent_id 未知时返回 None，由调用方决定降级策略
        """
        agent = self.registry.create_agent(
            agent_id=agent_id,
            session_id=session_id,
            agent_state=agent_state,
        )
        if agent is None:
            logger.warning(f"[AgentFactory] 无法为 agent_id={agent_id} 创建智能体")
        return agent

    def create_fallback(self, session_id: Optional[str] = None) -> Optional[Agent]:
        """降级兜底：意图识别失败时，强制走 general_agent。

        若 general_agent 也不存在则返回 None（极端情况）。
        """
        logger.info("[AgentFactory] 使用兜底智能体 general_agent")
        return self.registry.create_agent(
            agent_id="general_agent",
            session_id=session_id,
        )
