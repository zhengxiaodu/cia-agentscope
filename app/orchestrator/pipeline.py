"""流水线编排器：有关联·写死流程 → 按固定顺序串行执行。

适用场景：
- 有固定先后依赖的任务（如必须先查数据才能画图）

设计原则（来自文档）：能写死就写死，保证确定性，避免智能体自由发挥带来风险。
"""
import asyncio
import logging
from typing import AsyncGenerator, Dict, List, Optional

from agentscope.state import AgentState

from app.agents.factory import AgentFactory
from app.intent.models import IntentResult
from app.orchestrator.base import BaseOrchestrator, TaskResult

logger = logging.getLogger(__name__)


class PipelineOrchestrator(BaseOrchestrator):
    """写死流水线编排器。

    执行流程：
    1. 按意图列表的固定顺序串行执行
    2. 每步输出作为下一步的 prior_context
    3. 任一步失败 → 终止流水线
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        step_timeout: float = 60.0,
    ):
        super().__init__(agent_factory)
        self._step_timeout = step_timeout

    async def run(
        self,
        intent_result: IntentResult,
        session_id: Optional[str] = None,
        agent_states: Optional[Dict[str, AgentState]] = None,
    ) -> AsyncGenerator[str, None]:
        """按固定顺序串行执行意图。"""
        intents = intent_result.intents
        agent_states = agent_states or {}

        yield self._event({
            "type": "orchestration_start",
            "mode": "pipeline",
            "intent_count": len(intents),
        })

        # 串行执行流水线
        prior_context = ""
        self._last_results = []
        for i, intent in enumerate(intents):
            # 发送任务启动事件
            yield self._event({
                "type": "task_start",
                "intent_id": intent.id,
                "agent_id": intent.agent or "general_agent",
                "step": i + 1,
                "total_steps": len(intents),
            })

            # 加载该 agent 的已有状态
            agent_id = intent.agent or "general_agent"
            agent_state = agent_states.get(agent_id)

            # 执行当前步骤
            try:
                result = await asyncio.wait_for(
                    self._run_single_agent(
                        intent,
                        prior_context=prior_context,
                        session_id=session_id,
                        agent_state=agent_state,
                    ),
                    timeout=self._step_timeout,
                )
            except asyncio.TimeoutError:
                result = self._make_timeout_result(intent)

            self._last_results.append(result)

            # 回放 SSE 事件
            for event_str in result.events:
                yield event_str

            # 发送任务完成事件
            yield self._event({
                "type": "task_end",
                "intent_id": result.intent_id,
                "agent_id": result.agent_id,
                "success": result.success,
                "step": i + 1,
                "total_steps": len(intents),
            })

            # 检查执行失败 → 终止后续步骤
            if not result.success:
                yield self._event({
                    "type": "pipeline_intercept",
                    "step": i + 1,
                    "intent_id": intent.id,
                    "message": f"步骤 {i + 1}（{intent.id}）执行失败，流水线终止：{result.output}",
                })
                return

            # 传递输出给下一步
            prior_context += f"\n[步骤{i + 1} {intent.id} 的输出]\n{result.output}"

        # 流水线全部完成
        yield self._event({
            "type": "summary",
            "content": prior_context.strip(),
        })

    @staticmethod
    def _make_timeout_result(intent):
        """构造超时失败结果。"""
        return TaskResult(
            intent_id=intent.id,
            agent_id=intent.agent or "general_agent",
            success=False,
            output="步骤执行超时",
        )
