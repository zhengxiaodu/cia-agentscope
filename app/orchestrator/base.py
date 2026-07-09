"""编排器基础定义：TaskResult 数据类 + BaseOrchestrator 抽象类。"""
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, List, Optional

from pydantic import BaseModel, Field

from agentscope.event import AgentEvent, ReplyStartEvent
from agentscope.message import AssistantMsg, UserMsg
from agentscope.state import AgentState

from app.agents.factory import AgentFactory
from app.intent.models import Intent, IntentResult

logger = logging.getLogger(__name__)


class TaskResult(BaseModel):
    """单个意图执行完毕后的结果。

    Attributes:
        intent_id: 意图 id
        agent_id: 执行的智能体 id
        success: 是否成功
        output: 该智能体的文本输出（合并所有文本块）
        events: 该智能体产生的 SSE 事件字符串列表（用于回放）
        final_state: 执行结束后的 AgentState dict（用于持久化）
    """
    intent_id: str
    agent_id: str
    success: bool = True
    output: str = ""
    events: List[str] = Field(default_factory=list)
    final_state: Optional[dict] = None


class BaseOrchestrator(ABC):
    """编排器抽象基类。

    所有编排器共享的能力：
    - 创建智能体并执行（_run_single_agent）
    - 产生 SSE 事件（task_start / task_end / agent 事件透传）
    """

    def __init__(self, agent_factory: AgentFactory):
        self.agent_factory = agent_factory
        self._last_results: List[TaskResult] = []

    async def _run_single_agent(
        self,
        intent: Intent,
        prior_context: str = "",
        session_id: Optional[str] = None,
        agent_state: Optional[AgentState] = None,
    ) -> TaskResult:
        """执行单个智能体，收集所有 SSE 事件。

        Args:
            intent: 要执行的意图
            prior_context: 前置步骤的输出（流水线模式中使用）
            session_id: 会话 id
            agent_state: 已恢复的 AgentState（多轮上下文），为 None 则新建
        """
        agent_id = intent.agent or "general_agent"
        agent = self.agent_factory.create_for_agent(
            agent_id=agent_id,
            session_id=session_id,
            agent_state=agent_state,
        )
        if agent is None:
            return TaskResult(
                intent_id=intent.id,
                agent_id=agent_id,
                success=False,
                output=f"无法创建智能体 {agent_id}",
            )

        # 构建用户消息：如有前置上下文，附加在前
        user_content = intent.query
        if prior_context:
            user_content = f"[前置参考信息]\n{prior_context}\n\n[你的任务]\n{intent.query}"

        user_msg = UserMsg(name="user", content=user_content)

        result = TaskResult(intent_id=intent.id, agent_id=agent_id)
        apply = None

        try:
            async for event in agent.reply_stream(user_msg):
                if isinstance(event, ReplyStartEvent):
                    apply = AssistantMsg(name=event.name, content=[], id=event.reply_id)

                if isinstance(event, AgentEvent):
                    if apply:
                        apply.append_event(event)
                    # 收集事件用于回放
                    result.events.append(f"data: {event.model_dump_json()}\n\n")

            # 提取文本输出
            if apply:
                text_parts = []
                for block in apply.content:
                    if hasattr(block, "type") and block.type == "text":
                        text_parts.append(getattr(block, "text", str(block)))
                result.output = "\n".join(text_parts).strip()

            # 捕获执行后的 AgentState（用于后续持久化）
            result.final_state = agent.state.model_dump()

            result.success = True
        except Exception as e:
            logger.exception(f"[Orchestrator] 智能体 {agent_id} 执行异常")
            result.success = False
            result.output = f"执行出错: {str(e)}"

        return result

    @abstractmethod
    async def run(
        self,
        intent_result: IntentResult,
        session_id: Optional[str] = None,
        agent_states: Optional[Dict[str, AgentState]] = None,
    ) -> AsyncGenerator[str, None]:
        """执行编排，yield SSE 事件字符串。"""
        ...

    def _event(self, data: dict) -> str:
        """将 dict 序列化为 SSE 事件字符串。"""
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
