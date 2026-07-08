"""并行编排器：无关联多意图 → asyncio.gather 并行执行，汇总输出。

适用场景：
- 多个独立查询（如同时查新闻 + 查天气）
- 各意图互不依赖，可独立执行

设计原则（来自文档）：能并行就并行，减少用户等待时间。
"""
import asyncio
import logging
from typing import AsyncGenerator, Dict, List, Optional

from agentscope.state import AgentState

from app.agents.factory import AgentFactory
from app.intent.models import IntentResult
from app.orchestrator.base import BaseOrchestrator, TaskResult

logger = logging.getLogger(__name__)


class ParallelOrchestrator(BaseOrchestrator):
    """并行调度编排器。

    执行流程：
    1. asyncio.gather 并行执行所有意图对应的智能体
    2. 收集所有结果，汇总输出

    超时控制：每个智能体有独立超时（来自 intent_config.yml orchestrator.parallel_timeout）。
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        timeout: float = 60.0,
    ):
        super().__init__(agent_factory)
        self._timeout = timeout

    async def run(
        self,
        intent_result: IntentResult,
        session_id: Optional[str] = None,
        agent_states: Optional[Dict[str, AgentState]] = None,
    ) -> AsyncGenerator[str, None]:
        """并行执行所有意图，等全部完成后回放事件。"""
        intents = intent_result.intents
        agent_states = agent_states or {}

        # 发送编排开始事件
        yield self._event({
            "type": "orchestration_start",
            "mode": "parallel",
            "intent_count": len(intents),
        })

        # ① 并行执行所有意图（收集事件到局部缓冲，等全部完成后回放）
        tasks = [
            self._run_with_timeout(intent, session_id, agent_states)
            for intent in intents
        ]

        # 发送各任务启动事件
        for intent in intents:
            yield self._event({
                "type": "task_start",
                "intent_id": intent.id,
                "agent_id": intent.agent or "general_agent",
            })

        results: List[tuple] = await asyncio.gather(*tasks, return_exceptions=True)

        # 存储结果供后续保存状态用
        self._last_results = []

        # ② 回放事件 + 汇总结果
        summary_parts = []
        for i, item in enumerate(results):
            if isinstance(item, Exception):
                logger.exception(f"[ParallelOrchestrator] 意图 {intents[i].id} 执行异常")
                result = TaskResult(
                    intent_id=intents[i].id,
                    agent_id=intents[i].agent or "general_agent",
                    success=False,
                    output=f"执行异常: {str(item)}",
                )
                events = []
            else:
                result, events = item

            self._last_results.append(result)

            # 回放该智能体产生的 SSE 事件
            for event_str in events:
                yield event_str

            # 发送任务完成事件
            yield self._event({
                "type": "task_end",
                "intent_id": result.intent_id,
                "agent_id": result.agent_id,
                "success": result.success,
            })

            # 收集摘要
            if result.output:
                summary_parts.append(result.output)

        # ③ 汇总事件
        if len(summary_parts) > 1:
            summary = "\n\n---\n\n".join(summary_parts)
            yield self._event({
                "type": "summary",
                "content": f"已为您完成 {len(summary_parts)} 项任务：\n{summary}",
            })
        elif summary_parts:
            yield self._event({
                "type": "summary",
                "content": summary_parts[0],
            })

    async def _run_with_timeout(
        self,
        intent,
        session_id: Optional[str] = None,
        agent_states: Optional[Dict[str, AgentState]] = None,
    ) -> tuple[TaskResult, list[str]]:
        """带超时的智能体执行，收集事件到局部缓冲。

        Returns:
            (TaskResult, events_list)
        """
        agent_id = intent.agent or "general_agent"
        agent_state = (agent_states or {}).get(agent_id)

        result_box = []
        events = []
        try:
            async for event_str in asyncio.wait_for(
                self._consume_agent_events(intent, session_id, agent_state, result_box),
                timeout=self._timeout,
            ):
                events.append(event_str)
        except asyncio.TimeoutError:
            result_box.append(TaskResult(
                intent_id=intent.id,
                agent_id=agent_id,
                success=False,
                output="执行超时",
            ))

        result = result_box[0] if result_box else TaskResult(
            intent_id=intent.id,
            agent_id=agent_id,
            success=False,
            output="未获取到执行结果",
        )
        return result, events

    async def _consume_agent_events(
        self,
        intent,
        session_id: Optional[str],
        agent_state: Optional[AgentState],
        result_box: list,
    ) -> AsyncGenerator[str, None]:
        """包装 _run_single_agent，将事件转为可被 wait_for 的生成器。"""
        async for event_str in self._run_single_agent(
            intent,
            session_id=session_id,
            agent_state=agent_state,
            result_box=result_box,
        ):
            yield event_str
