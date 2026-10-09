"""流水线编排器：有关联·写死流程 → 按固定顺序串行执行。

适用场景：
- 有固定先后依赖的任务（如必须先查数据才能画图）

设计原则（来自文档）：能写死就写死，保证确定性，避免智能体自由发挥带来风险。
"""
import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from agentscope.state import AgentState

from app.agents.factory import AgentFactory
from app.intent.models import IntentResult
from app.orchestrator.base import BaseOrchestrator, TaskResult
from app.utils.trace_names import TraceName

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
        langfuse_service: Optional[Any] = None,
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

        # P1-6: 编排级 span
        _pl_obs = None
        _stopped_at = None
        if langfuse_service and getattr(langfuse_service, "enabled", False):
            try:
                _pl_obs = langfuse_service.start_observation(
                    name=TraceName.ORCHESTRATE_PIPELINE, as_type="span",
                    input={"totalSteps": len(intents)})
            except Exception:
                _pl_obs = None

        def _close_pipeline_span(success: bool):
            if _pl_obs is None:
                return
            try:
                langfuse_service.end_observation(_pl_obs, output={
                    "totalSteps": len(intents),
                    "stoppedAtStep": _stopped_at,
                    "success": success,
                })
            except Exception:
                pass

        try:
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

                # 执行当前步骤（实时透传事件 + 超时控制）
                result = None
                try:
                    async with asyncio.timeout(self._step_timeout):
                        async for item in self._run_single_agent(
                            intent,
                            prior_context=prior_context,
                            session_id=session_id,
                            agent_state=agent_state,
                            langfuse_service=langfuse_service,
                        ):
                            if isinstance(item, TaskResult):
                                result = item
                            else:
                                # 实时透传 SSE 事件
                                yield item
                except asyncio.TimeoutError:
                    result = self._make_timeout_result(intent)

                # 若未拿到 result（理论上不会），兜底
                if result is None:
                    result = self._make_timeout_result(intent)

                self._last_results.append(result)

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
                    _stopped_at = i + 1
                    yield self._event({
                        "type": "pipeline_intercept",
                        "step": i + 1,
                        "intent_id": intent.id,
                        "message": f"步骤 {i + 1}（{intent.id}）执行失败，流水线终止：{result.output}",
                    })
                    _close_pipeline_span(success=False)
                    return

                # 传递输出给下一步
                prior_context = f"\n{result.output}"

            _stopped_at = len(intents)
            _close_pipeline_span(success=True)
            # 流水线全部完成
            yield self._event({
                "type": "summary",
                "content": prior_context.strip(),
            })
        except Exception:
            _close_pipeline_span(success=False)
            raise

    @staticmethod
    def _make_timeout_result(intent):
        """构造超时失败结果。"""
        return TaskResult(
            intent_id=intent.id,
            agent_id=intent.agent or "general_agent",
            success=False,
            output="步骤执行超时",
        )
