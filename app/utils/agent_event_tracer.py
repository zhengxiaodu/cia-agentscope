"""把 AgentScope 事件流旁路翻译成 Langfuse 子 observation。

AgentScope 已在事件里给出模型名与真实 token 用量，事件循环原本只转 SSE 就丢弃。
本类在同一循环里旁路消费，把 LLM 调用、工具调用补成可下钻子 observation。
不用 register_hook、不包装 OpenAIChatModel。

配对约定（实测报文验证）：同一 reply_id 下会串行发起多次模型调用，
按 reply_id 维护"当前未结束的 generation"，START 时若存在悬挂项先行结束，防泄漏。
"""
import logging
from typing import Any, Dict, Optional

from agentscope.event import (
    ExceedMaxItersEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    TextBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)

logger = logging.getLogger(__name__)


class AgentEventTracer:
    """单次 agent 执行期间的事件翻译器，与 agent span 生命周期一致。"""

    def __init__(self, langfuse_service: Optional[Any], agent_id: str) -> None:
        self._lf = langfuse_service
        self._agent_id = agent_id
        self._model_obs: Dict[str, Any] = {}
        self._model_name: Dict[str, str] = {}
        self._text_buf: Dict[str, list] = {}
        self._tool_obs: Dict[str, Any] = {}
        self._tool_name: Dict[str, str] = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_calls = 0
        self.tool_calls = 0
        self.tool_failures = 0
        self.exceed_max_iters = False

    @property
    def _enabled(self) -> bool:
        return bool(self._lf is not None and getattr(self._lf, "enabled", False))

    def on_event(self, event: Any) -> None:
        if not self._enabled:
            return
        try:
            self._dispatch(event)
        except Exception:
            logger.debug("[AgentEventTracer] 事件埋点失败", exc_info=True)

    def close(self) -> None:
        if not self._enabled:
            return
        for reply_id in list(self._model_obs):
            self._end_model(reply_id, None, None, "interrupted")
        for tool_call_id in list(self._tool_obs):
            self._end_tool(tool_call_id, "interrupted")

    def summary(self) -> dict:
        return {
            "llmCalls": self.llm_calls,
            "toolCalls": self.tool_calls,
            "toolFailures": self.tool_failures,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "exceedMaxIters": self.exceed_max_iters,
        }

    def _dispatch(self, event: Any) -> None:
        if isinstance(event, ModelCallStartEvent):
            self._end_model(event.reply_id, None, None, "dangling")
            self._model_name[event.reply_id] = event.model_name
            self._text_buf[event.reply_id] = []
            self._model_obs[event.reply_id] = self._lf.start_observation(
                name=f"llm-{event.model_name}", as_type="generation",
                model=event.model_name,
                metadata={"agentId": self._agent_id, "replyId": event.reply_id},
            )
            self.llm_calls += 1
        elif isinstance(event, ModelCallEndEvent):
            self.input_tokens += event.input_tokens or 0
            self.output_tokens += event.output_tokens or 0
            self._end_model(
                event.reply_id, event.input_tokens, event.output_tokens,
                getattr(event.finished_reason, "value", str(event.finished_reason)),
            )
        elif isinstance(event, TextBlockDeltaEvent):
            buf = self._text_buf.get(event.reply_id)
            if buf is not None and len(buf) < 2000:
                buf.append(event.delta or "")
        elif isinstance(event, ToolCallStartEvent):
            self._tool_name[event.tool_call_id] = event.tool_call_name
            self._tool_obs[event.tool_call_id] = self._lf.start_observation(
                name=f"tool-{event.tool_call_name}", as_type="tool",
                metadata={"agentId": self._agent_id, "toolCallId": event.tool_call_id},
            )
            self.tool_calls += 1
        elif isinstance(event, ToolResultEndEvent):
            state = getattr(event.state, "value", str(event.state))
            if state == "error":
                self.tool_failures += 1
            self._end_tool(event.tool_call_id, state)
        elif isinstance(event, ExceedMaxItersEvent):
            self.exceed_max_iters = True

    def _end_model(self, reply_id, input_tokens, output_tokens, finished_reason) -> None:
        obs = self._model_obs.pop(reply_id, None)
        if obs is None:
            return
        output_text = "".join(self._text_buf.pop(reply_id, []))
        usage = None
        if input_tokens is not None or output_tokens is not None:
            usage = {"input": input_tokens or 0, "output": output_tokens or 0}
        self._lf.end_observation(
            obs, output={"text": output_text[:4000], "finishedReason": finished_reason},
            usage_details=usage,
        )
        self._model_name.pop(reply_id, None)

    def _end_tool(self, tool_call_id, state) -> None:
        obs = self._tool_obs.pop(tool_call_id, None)
        if obs is None:
            return
        self._lf.end_observation(
            obs, output={"state": state, "tool": self._tool_name.pop(tool_call_id, "")},
            level="ERROR" if state == "error" else None,
        )
