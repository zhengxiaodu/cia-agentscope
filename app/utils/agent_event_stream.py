"""reply_stream 事件循环公共 helper，base.py 与 orchestrator_service.py 单 agent 路径共用。

只抽循环本体：消费 reply_stream → ReplyStart 走回调 → AgentEvent 喂 tracer 后 yield。
span 创建与收尾、append_event、model_dump_json 均留在调用方（两处收尾逻辑不同）。
"""
from typing import Any, AsyncGenerator, Callable

from agentscope.event import AgentEvent, ReplyStartEvent


async def iter_agent_events(
    agent: Any,
    user_msg: Any,
    tracer: Any,
    on_reply_start: Callable[[Any], None],
) -> AsyncGenerator[Any, None]:
    """yield 每个 AgentEvent（原始对象）。调用方负责 append_event + model_dump_json。"""
    async for event in agent.reply_stream(user_msg):
        if isinstance(event, ReplyStartEvent):
            on_reply_start(event)
        if isinstance(event, AgentEvent):
            tracer.on_event(event)
            yield event
