"""编排器基础定义：TaskResult 数据类 + BaseOrchestrator 抽象类。"""
import json
import logging
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack, aclosing, contextmanager
from typing import Any, AsyncGenerator, Dict, Iterator, List, Optional, Union

from pydantic import BaseModel, Field

from agentscope.event import AgentEvent, ReplyStartEvent
from agentscope.message import AssistantMsg, TextBlock, UserMsg
from agentscope.state import AgentState

from app.agents.factory import AgentFactory
from app.intent.models import Intent, IntentResult

logger = logging.getLogger(__name__)


@contextmanager
def _noop_ctx() -> Iterator[None]:
    """空 context manager，langfuse 未启用时作为 span 占位，yield None。"""
    yield None


def _is_context_length_error(exc: Exception) -> bool:
    """判断异常是否为模型上下文超长类错误（400）。

    匹配多种引擎/SDK 的错误文本：OpenAI BadRequestError、私有引擎
    "maximum context length" / "context_length_exceeded" / "token limit" 等。
    命中时可清空历史上下文后重试一次，避免整轮对话直接中断。
    """
    msg = str(exc).lower()
    keywords = (
        "context_length_exceeded",
        "maximum context length",
        "context window",
        "token limit",
        "too many tokens",
        "input too long",
        "request too large",
        "exceeds maximum",          # 私有引擎：exceeds maximum allowed limit
        "input length",             # 私有引擎：Input length (X) exceeds ...
        "error code: 400",          # OpenAI BadRequestError 通用兜底
    )
    return any(k in msg for k in keywords)


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
    metadata: Optional[dict] = None


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
        langfuse_service: Optional[Any] = None,
    ) -> AsyncGenerator[Union[str, "TaskResult"], None]:
        """执行单个智能体，实时 yield SSE 事件，最后 yield TaskResult。

        Args:
            intent: 要执行的意图
            prior_context: 前置步骤的输出（流水线模式中使用）
            session_id: 会话 id
            agent_state: 已恢复的 AgentState（多轮上下文），为 None 则新建
            langfuse_service: 可选的 Langfuse 服务，用于启动 agent 调用子 span

        Yields:
            str: SSE 事件字符串（实时透传，形如 "data: {...}\\n\\n"）
            TaskResult: 最后一个 yield，含 output/final_state/success（events 留空）
        """
        agent_id = intent.agent or "general_agent"
        agent = self.agent_factory.create_for_agent(
            agent_id=agent_id,
            session_id=session_id,
            agent_state=agent_state,
        )
        if agent is None:
            yield TaskResult(
                intent_id=intent.id,
                agent_id=agent_id,
                success=False,
                output=f"无法创建智能体 {agent_id}",
            )
            return

        # 构建用户消息：如有前置上下文，附加在前
        user_content = intent.query
        if prior_context:
            user_content = f"[前置参考信息]\n{prior_context}\n\n[你的任务]\n{intent.query}"

        user_msg = UserMsg(name="user", content=[TextBlock(text=user_content)])

        result = TaskResult(intent_id=intent.id, agent_id=agent_id)
        apply = None

        # 环节埋点：每次智能体调用子 span
        # 从 agent 对象读取 system_prompt（agentscope ReActAgent 通过 sys_prompt 属性暴露）
        agent_sys_prompt = getattr(agent, "sys_prompt", None) or ""
        span_ctx = (
            langfuse_service.start_span(
                f"agent-{agent_id}",
                input={
                    "intent": intent.id,
                    # 信息精简：query 注入的文件全文在 upload_files.parsed_content 留底，
                    # system_prompt 全文以仓库/配置为准（llm span 另有前 1000 字符副本）
                    "query": intent.query[:2000],
                    "system_prompt": agent_sys_prompt[:1000],
                    "user_message": user_content[:2000],
                },
            )
            if langfuse_service
            else _noop_ctx()
        )
        async with AsyncExitStack() as stream_stack:
            agent_span = stream_stack.enter_context(span_ctx)
            from app.utils.agent_event_tracer import AgentEventTracer
            from app.utils.agent_event_stream import iter_agent_events
            from app.utils.event_serialization import serialize_agent_event
            from agentscope.event import ToolResultEndEvent as _ToolResultEnd
            from tools.bocha_search_tools import merge_bocha_snapshots
            tracer = AgentEventTracer(langfuse_service, agent_id, agent=agent)
            # 已下发 bocha_sum 累积：按 citation_id 幂等合并、升序，
            # 每次事件发"全量快照"而非增量，多次搜索时前端覆盖式渲染仍得到完整来源列表
            bocha_emitted: list = []
            # 上下文超长降级重试：最多重试 1 次，重试前清空历史对话上下文
            # （保留压缩摘要 summary），避免 400 直接中断整轮对话
            max_retries = 1
            for attempt in range(max_retries + 1):
                try:
                    def _on_reply_start(ev):
                        nonlocal apply
                        apply = AssistantMsg(name=ev.name, content=[], id=ev.reply_id)

                    events = await stream_stack.enter_async_context(aclosing(
                        iter_agent_events(agent, user_msg, tracer, _on_reply_start)
                    ))
                    async for event in events:
                        if apply:
                            apply.append_event(event)
                        # 实时透传事件给上层
                        yield f"data: {serialize_agent_event(event)}\n\n"
                        # 工具一结束立即下发 bocha_sum：正文 {n} 标注依赖来源列表，
                        # 先于正文 delta 到达前端，便于边渲染正文边映射引用
                        if isinstance(event, _ToolResultEnd):
                            early_bocha = tracer.consume_bocha_sum()
                            if early_bocha:
                                bocha_emitted = merge_bocha_snapshots(bocha_emitted, early_bocha)
                                yield (
                                    "data: "
                                    + json.dumps(
                                        {"type": "bocha_sum", "bocha_sum": bocha_emitted},
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                )

                    # 提取文本输出
                    if apply:
                        text_parts = []
                        for block in apply.content:
                            if hasattr(block, "type") and block.type == "text":
                                text_parts.append(getattr(block, "text", str(block)))
                        result.output = "\n".join(text_parts).strip()

                    # 捕获执行后的 AgentState（用于后续持久化）
                    # mode="json" 确保枚举等类型序列化为值，避免落库后无法反序列化
                    result.final_state = agent.state.model_dump(mode="json")

                    result.success = True
                    break  # 成功，跳出重试循环
                except Exception as e:
                    # 上下文超长（400）：清空历史对话上下文（保留 summary）后重试一次
                    if _is_context_length_error(e) and attempt < max_retries:
                        logger.warning(
                            "[Orchestrator] 智能体 %s 上下文超长，"
                            "清空历史上下文后重试（第 %d 次）: %s",
                            agent_id, attempt + 1, e,
                        )
                        try:
                            agent.state.context.clear()
                        except Exception:
                            logger.warning(
                                "[Orchestrator] 清空 agent.state.context 失败",
                                exc_info=True,
                            )
                        # 重置 apply，避免上次失败的部分结果污染
                        apply = None
                        continue
                    logger.exception(f"[Orchestrator] 智能体 {agent_id} 执行异常")
                    result.success = False
                    result.output = f"执行出错: {str(e)}"
                finally:
                    tracer.close()

            if agent_span:
                try:
                    agent_span.update(output={
                        "output": result.output,
                        "success": result.success,
                        **tracer.summary(),
                    })
                except Exception:
                    pass

        # emit 工具执行期间捕获的 citations（从 ToolResultEndEvent.metadata 累积）
        citations = tracer.consume_citations()
        if citations:
            yield (
                "data: "
                + json.dumps(
                    {"type": "policy_qa_citations", "citations": citations},
                    ensure_ascii=False,
                )
                + "\n\n"
            )

        # emit 流末残余的 bocha_sum：残余快照按 citation_id 合并，无新编号时内容不变；
        # 已发过且无残余时不再重复发（最后一个事件已含全量）
        residual_bocha = tracer.consume_bocha_sum()
        if residual_bocha:
            bocha_emitted = merge_bocha_snapshots(bocha_emitted, residual_bocha)
            yield (
                "data: "
                + json.dumps(
                    {"type": "bocha_sum", "bocha_sum": bocha_emitted},
                    ensure_ascii=False,
                )
                + "\n\n"
            )

        yield result

    @abstractmethod
    async def run(
        self,
        intent_result: IntentResult,
        session_id: Optional[str] = None,
        agent_states: Optional[Dict[str, AgentState]] = None,
        langfuse_service: Optional[Any] = None,
    ) -> AsyncGenerator[str, None]:
        """执行编排，yield SSE 事件字符串。"""
        ...

    def _event(self, data: dict) -> str:
        """将 dict 序列化为 SSE 事件字符串。"""
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
