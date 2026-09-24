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
from openai import AsyncOpenAI

from app.agents.factory import AgentFactory
from app.intent.llm_client import chat_complete
from app.intent.models import IntentResult
from app.orchestrator.base import BaseOrchestrator, TaskResult
from app.utils.trace_names import TraceName

logger = logging.getLogger(__name__)

# 单个任务输出进入汇总 prompt 的截断上限（防超上下文）
_MAX_TASK_OUTPUT_CHARS = 4000

# 汇总 LLM 系统提示：整合多任务结果为一份连贯回答
_SUMMARY_SYSTEM_PROMPT = (
    "你是一个多任务结果汇总助手。多个智能体分别完成了用户问题中的不同子任务，"
    "请将它们的结果整合为一份连贯、完整的最终回答。要求：\n"
    "1. 按主题整合内容形成连贯回答，而不是逐个罗列任务结果\n"
    "2. 保留各任务的关键信息和数据，不要遗漏\n"
    "3. 对失败的任务如实简要说明其未能完成\n"
    "4. 不编造任何任务结果中不存在的信息\n"
    "5. 直接输出汇总结果，不要寒暄"
)


class ParallelOrchestrator(BaseOrchestrator):
    """并行调度编排器。

    执行流程：
    1. asyncio.gather 并行执行所有意图对应的智能体
    2. 收集所有结果，多结果时调用 LLM 汇总为一份连贯回答（失败/超时回退机械拼接）

    超时控制：每个智能体有独立超时（来自 intent_config.yml orchestrator.parallel_timeout）。
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        timeout: float = 60.0,
        summary_client: Optional[AsyncOpenAI] = None,
        summary_model_config: Optional[dict] = None,
        summary_timeout: float = 60.0,
    ):
        """
        Args:
            agent_factory: 智能体工厂
            timeout: 单智能体执行超时（秒）
            summary_client: 汇总用 LLM 客户端（默认业务大模型），为空时回退机械拼接
            summary_model_config: models.default 配置段
            summary_timeout: 汇总 LLM 调用超时（秒）
        """
        super().__init__(agent_factory)
        self._timeout = timeout
        self._summary_client = summary_client
        self._summary_model_config = summary_model_config
        self._summary_timeout = summary_timeout

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

        # ③ 汇总事件（多结果一律 LLM 汇总，异常兜底回退拼接）
        if len(summary_parts) > 1:
            yield self._event({
                "type": "parallel_summary", "status": "started",
                "message": "正在汇总各任务结果...",
            })
            summary = await self._summarize(intent_result, self._last_results)
            if summary:
                yield self._event({"type": "summary", "content": summary})
            else:  # LLM 失败/超时兜底：机械拼接
                summary = "\n\n---\n\n".join(summary_parts)
                yield self._event({
                    "type": "summary",
                    "content": f"已为您完成 {len(summary_parts)} 项任务：\n{summary}",
                })
        elif summary_parts:
            yield self._event({"type": "summary", "content": summary_parts[0]})

    @staticmethod
    def _build_summary_prompt(
        intent_result: IntentResult, results: List[TaskResult],
    ) -> str:
        """构造汇总 prompt：用户问题 + 逐个任务结果块（超长截断）。"""
        parts = [f"用户问题：{intent_result.rewritten_query}", "", "各任务执行结果如下："]
        for i, r in enumerate(results, start=1):
            status = "成功" if r.success else "失败"
            output = r.output or ""
            if len(output) > _MAX_TASK_OUTPUT_CHARS:
                output = output[:_MAX_TASK_OUTPUT_CHARS] + "...（内容过长已截断）"
            parts.append(f"【任务{i} - {r.intent_id}（智能体：{r.agent_id}）- {status}】")
            parts.append(output)
        return "\n".join(parts)

    async def _summarize(
        self, intent_result: IntentResult, results: List[TaskResult],
    ) -> Optional[str]:
        """调用 LLM 汇总多任务结果为一份连贯回答。

        客户端未注入（直接构造/测试场景）、调用失败或超时均返回 None，
        由调用方回退机械拼接。
        """
        if not (self._summary_client and self._summary_model_config):
            return None
        user_prompt = self._build_summary_prompt(intent_result, results)
        try:
            async with asyncio.timeout(self._summary_timeout):
                text = await chat_complete(
                    self._summary_client,
                    self._summary_model_config,
                    _SUMMARY_SYSTEM_PROMPT,
                    user_prompt,
                    stage=str(TraceName.LLM_PARALLEL_SUMMARY),
                )
            return text.strip() or None
        except Exception as e:
            logger.warning(f"[ParallelOrchestrator] 汇总 LLM 调用失败: {e}")
            return None
