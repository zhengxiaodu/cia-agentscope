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
    ToolCallDeltaEvent,
    ToolResultTextDeltaEvent
)

logger = logging.getLogger(__name__)


class AgentEventTracer:
    """单次 agent 执行期间的事件翻译器，与 agent span 生命周期一致。"""

    def __init__(
        self,
        langfuse_service: Optional[Any],
        agent_id: str,
        agent: Optional[Any] = None,
    ) -> None:
        self._lf = langfuse_service
        self._agent_id = agent_id
        self._agent = agent
        self._model_obs: Dict[str, Any] = {}
        self._model_name: Dict[str, str] = {}
        self._text_buf: Dict[str, list] = {}
        self._tool_obs: Dict[str, Any] = {}
        self._tool_name: Dict[str, str] = {}
        # 工具入参 JSON 增量与返回文本增量（按 tool_call_id 累积，
        # 用于 tool span 的 input/output 完整记录）
        self._tool_args_buf: Dict[str, list] = {}
        self._tool_result_buf: Dict[str, list] = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_calls = 0
        self.tool_calls = 0
        self.tool_failures = 0
        self.exceed_max_iters = False
        # 累积从 ToolResultEndEvent.metadata 提取的 citations（制度问答引用），
        # 供调用方 emit 自定义事件流。独立于 langfuse 启用状态。
        self._collected_citations: list = []
        # 累积从 ToolResultEndEvent.metadata 提取的 bocha_sum（博查搜索来源摘要），
        # 与 citations 同机制旁路提取。
        self._collected_bocha_sum: list = []

    @property
    def _enabled(self) -> bool:
        return bool(self._lf is not None and getattr(self._lf, "enabled", False))

    def on_event(self, event: Any) -> None:
        # citations 累积不依赖 langfuse 启用状态，先于埋点逻辑执行
        self._collect_citations(event)
        if not self._enabled:
            return
        try:
            self._dispatch(event)
        except Exception:
            logger.debug("[AgentEventTracer] 事件埋点失败", exc_info=True)

    def _collect_citations(self, event: Any) -> None:
        """从 ToolResultEndEvent.metadata 提取 citations 与 bocha_sum 累积。

        agentscope 主路径会把 ToolChunk.metadata 透传到
        ToolResultEndEvent.metadata，任何工具把 citations / bocha_sum
        写进 ToolChunk.metadata 都能在此被提取。
        """
        try:
            if not isinstance(event, ToolResultEndEvent):
                return
            meta = getattr(event, "metadata", None) or {}
            if not isinstance(meta, dict):
                return
            meta_citations = meta.get("citations")
            if isinstance(meta_citations, list) and meta_citations:
                self._collected_citations.extend(meta_citations)
            meta_bocha = meta.get("bocha_sum")
            if isinstance(meta_bocha, list) and meta_bocha:
                # 原样收集（不去重）：URL 去重与编号由工具的请求级来源表
                # 统一负责，快照条目携带 citation_id，由下游按编号幂等合并
                self._collected_bocha_sum.extend(meta_bocha)
        except Exception:
            logger.debug("[AgentEventTracer] 提取 citations 失败", exc_info=True)

    def consume_citations(self) -> list:
        """返回并清空累积的 citations（供调用方 emit 自定义事件流）。

        在 agent 事件流结束后调用；tracer.close() 不影响此方法
        （close 只关闭 langfuse observation，不清空 citations）。
        """
        if not self._collected_citations:
            return []
        result = self._collected_citations
        self._collected_citations = []
        return result

    def consume_bocha_sum(self) -> list:
        """返回并清空累积的 bocha_sum（供调用方 emit 自定义事件流）。

        与 consume_citations 对称：在 agent 事件流结束后调用。
        """
        if not self._collected_bocha_sum:
            return []
        result = self._collected_bocha_sum
        self._collected_bocha_sum = []
        return result

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

    @staticmethod
    def _extract_content(content: Any) -> str:
        """Msg.content（str 或 block 列表）→ 纯文本。"""
        if isinstance(content, str):
            return content
        parts = []
        for b in content or []:
            if isinstance(b, dict):
                text = b.get("text") or b.get("output") or b.get("type") or ""
            else:
                text = (
                    getattr(b, "text", None)
                    or getattr(b, "output", None)
                    or type(b).__name__
                )
            parts.append(str(text))
        return " ".join(parts)

    def _serialize_model_input(self) -> Optional[dict]:
        """从 agent state 提取本次模型调用的输入（系统提示 + 上下文消息）。

        记录最近 30 条、每条截断 800 字符，控制 Langfuse 记录体积；
        agent 未注入时返回 None（不影响原有行为）。
        """
        agent = self._agent
        if agent is None:
            return None
        try:
            state = getattr(agent, "state", None)
            context = list(getattr(state, "context", None) or [])
            messages = []
            for m in context[-30:]:
                if hasattr(m, "model_dump"):
                    d = m.model_dump(mode="json")
                else:
                    d = {
                        "role": getattr(m, "role", ""),
                        "content": getattr(m, "content", ""),
                    }
                messages.append({
                    "role": str(d.get("role", "")),
                    "content": self._extract_content(d.get("content"))[:800],
                })
            system_prompt = getattr(agent, "_system_prompt", "") or ""
            return {
                "systemPrompt": str(system_prompt)[:1000],
                "messages": messages,
            }
        except Exception:
            logger.debug("[AgentEventTracer] 序列化模型输入失败", exc_info=True)
            return None

    def _dispatch(self, event: Any) -> None:
        if isinstance(event, ModelCallStartEvent):
            self._end_model(event.reply_id, None, None, "dangling")
            self._model_name[event.reply_id] = event.model_name
            self._text_buf[event.reply_id] = []
            self._model_obs[event.reply_id] = self._lf.start_observation(
                name=f"llm-{event.model_name}", as_type="generation",
                model=event.model_name,
                input=self._serialize_model_input(),
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
            self._tool_args_buf[event.tool_call_id] = []
            self._tool_result_buf[event.tool_call_id] = []
            self.tool_calls += 1
        elif isinstance(event, ToolCallDeltaEvent):
            # 累积工具入参 JSON 增量（Start 后流式到达，End 时拼装进 input）
            args_buf = self._tool_args_buf.get(event.tool_call_id)
            if args_buf is not None and sum(map(len, args_buf)) < 2000:
                args_buf.append(event.delta or "")
        elif isinstance(event, ToolResultTextDeltaEvent):
            # 累积工具返回文本增量（End 前流式到达，End 时拼装进 output）
            res_buf = self._tool_result_buf.get(event.tool_call_id)
            if res_buf is not None and sum(map(len, res_buf)) < 4000:
                res_buf.append(event.delta or "")
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
        args = "".join(self._tool_args_buf.pop(tool_call_id, []))
        result = "".join(self._tool_result_buf.pop(tool_call_id, []))
        if obs is None:
            return
        self._lf.end_observation(
            obs,
            input={"arguments": args[:2000]} if args else None,
            output={
                "state": state,
                "tool": self._tool_name.pop(tool_call_id, ""),
                "result": result[:4000],
            },
            level="ERROR" if state == "error" else None,
        )
