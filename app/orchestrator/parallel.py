"""并行编排器：无关联多意图 → asyncio.gather 并行执行，汇总输出。

适用场景：
- 多个独立查询（如同时查新闻 + 查天气）
- 各意图互不依赖，可独立执行

设计原则（来自文档）：能并行就并行，减少用户等待时间。
"""
import asyncio
import json
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
        """并行执行所有意图。"""
        intents = intent_result.intents
        agent_states = agent_states or {}

        # 发送编排开始事件
        yield self._event({
            "type": "orchestration_start",
            "mode": "parallel",
            "intent_count": len(intents),
        })

        # ① 并行执行所有意图
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

        results: List[TaskResult] = await asyncio.gather(*tasks, return_exceptions=True)

        # 存储结果供后续保存状态用
        self._last_results = [
            r for r in results if isinstance(r, TaskResult)
        ]

        # ② 回放事件 + 汇总结果
        summary_parts = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.exception(f"[ParallelOrchestrator] 意图 {intents[i].id} 执行异常")
                result = TaskResult(
                    intent_id=intents[i].id,
                    agent_id=intents[i].agent or "general_agent",
                    success=False,
                    output=f"执行异常: {str(result)}",
                )

            # 回放该智能体产生的 SSE 事件
            for event_str in result.events:
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
    ) -> TaskResult:
        """带超时的智能体执行。"""
        agent_id = intent.agent or "general_agent"
        agent_state = (agent_states or {}).get(agent_id)
        return await asyncio.wait_for(
            self._run_single_agent(intent, session_id=session_id, agent_state=agent_state),
            timeout=self._timeout,
        )
