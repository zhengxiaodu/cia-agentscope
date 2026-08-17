"""并行编排器：无关联多意图 → asyncio.gather 并行执行，汇总输出。

适用场景：
- 多个独立查询（如同时查新闻 + 查天气）
- 各意图互不依赖，可独立执行

设计原则（来自文档）：能并行就并行，减少用户等待时间。
"""
import asyncio
import json
import logging
import traceback
from typing import Any, AsyncGenerator, Dict, List, Optional

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
        langfuse_service: Optional[Any] = None,
    ) -> AsyncGenerator[str, None]:
        """并行执行所有意图，事件实时交错透传。"""
        intents = intent_result.intents
        agent_states = agent_states or {}

        # 发送编排开始事件
        yield self._event({
            "type": "orchestration_start",
            "mode": "parallel",
            "intent_count": len(intents),
        })

        # 发送各任务启动事件
        for intent in intents:
            yield self._event({
                "type": "task_start",
                "intent_id": intent.id,
                "agent_id": intent.agent or "general_agent",
            })

        # ① 用 asyncio.Queue 汇聚多 agent 事件，实现交错实时透传
        queue: asyncio.Queue = asyncio.Queue()
        # 用哨兵 None 标记某个 agent 的流结束
        SENTINEL = object()

        async def runner(intent):
            """单个 agent 的执行协程：把事件/result 推入队列，超时则推失败 result。"""
            agent_id = intent.agent or "general_agent"
            agent_state = (agent_states or {}).get(agent_id)
            try:
                async with asyncio.timeout(self._timeout):
                    async for item in self._run_single_agent(
                        intent, session_id=session_id, agent_state=agent_state,
                        langfuse_service=langfuse_service,
                    ):
                        await queue.put(item)
            except asyncio.TimeoutError:
                await queue.put(TaskResult(
                    intent_id=intent.id,
                    agent_id=agent_id,
                    success=False,
                    output="执行超时",
                ))
            except Exception as e:
                logger.exception(
                    f"[ParallelOrchestrator] 意图 {intent.id} 执行异常"
                )
                await queue.put(TaskResult(
                    intent_id=intent.id,
                    agent_id=agent_id,
                    success=False,
                    output=f"执行异常: {str(e)}",
                    metadata={
                        "errorClass": f"{type(e).__module__}.{type(e).__name__}",
                        "traceback": traceback.format_exc()[-1000:],
                    },
                ))
            finally:
                await queue.put(SENTINEL)

        # 启动所有 runner task
        runner_tasks = [
            asyncio.create_task(runner(intent)) for intent in intents
        ]

        # ② 主循环：从 queue 取 item，事件实时 yield，TaskResult 收集 + 发 task_end
        self._last_results = []
        finished = 0
        total = len(intents)
        summary_parts = []
        while finished < total:
            item = await queue.get()
            if item is SENTINEL:
                finished += 1
                continue
            if isinstance(item, TaskResult):
                self._last_results.append(item)
                # 发送任务完成事件
                yield self._event({
                    "type": "task_end",
                    "intent_id": item.intent_id,
                    "agent_id": item.agent_id,
                    "success": item.success,
                })
                if item.output:
                    summary_parts.append(item.output)
            else:
                # 实时透传 SSE 事件
                yield item

        # 等待所有 runner task 结束（消费可能的异常，避免未检索警告）
        await asyncio.gather(*runner_tasks, return_exceptions=True)

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
