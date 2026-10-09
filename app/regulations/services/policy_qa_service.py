"""制度问答服务（policy-qa）— 无状态 blocking / SSE streaming 编排层。

自 qa_app/services/chat_service.py 的 run_general_qa / run_general_qa_stream
移植：去掉会话/消息落库（sqlalchemy）与标题/建议问题生成，保留 LangGraph
编排（RunnableConfig 注入 llm / kb_client / thread_id）、SSE 事件映射、
Langfuse 埋点与空应答知识缺口后台记录。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator

from fastapi.responses import StreamingResponse
from langgraph.types import RunnableConfig

from app.regulations.config import (
    REGULATIONS_RERANK_TOP_K_OVERRIDE,
    REGULATIONS_TOP_K_OVERRIDE,
)
from app.regulations.graph.qa_graph import build_qa_graph, set_model_cfg
from app.regulations.graph.state import QAState
from app.regulations.providers.langfuse import get_langfuse
from app.regulations.providers.llm import build_llm, resolve_thinking
from app.regulations.providers.trace import new_serial, set_serial
from app.regulations.schemas import GeneralQAResponse, RetrieverResource
from app.regulations.services.kb_client import KBClient
from app.regulations.services.knowledge_gap_service import KnowledgeGapService
from app.regulations.services.langfuse_meta import build_cited_docs, compute_result_type

logger = logging.getLogger(__name__)


class PolicyQAService:
    """制度问答服务（/api/v1/policy/qa）：无状态 blocking / streaming。

    - 组装 LangGraph 图，通过 RunnableConfig 注入 llm / kb_client
    - streaming 模式映射 astream_events → SSE 事件
    - Langfuse 埋点 + 空应答知识缺口后台记录
    """

    def __init__(self, model_cfg: dict, kb_client: KBClient, gap_service: KnowledgeGapService):
        # understand/refuse 节点构建改写 LLM 时读取模块级模型配置，须先注入
        set_model_cfg(model_cfg)
        self.model_cfg = model_cfg
        self.kb_client = kb_client
        self.gap_service = gap_service
        self.graph = build_qa_graph()
        # 后台任务强引用（缺口记录等），防止 pending task 被 GC 静默回收
        self._pending_tasks: list[asyncio.Task] = []

    def _spawn(self, coro) -> asyncio.Task:
        """创建后台任务并持强引用，完成/失败后移除。"""
        task = asyncio.create_task(coro)

        def _done(t: asyncio.Task) -> None:
            try:
                self._pending_tasks.remove(t)
            except ValueError:
                pass

        task.add_done_callback(_done)
        self._pending_tasks.append(task)
        return task

    def _spawn_gap_record(self, query: str, kb_ids: list[str], result_type: str) -> None:
        """空应答时后台落一条知识缺口工单（不阻塞主流程）。"""
        if result_type != "empty" or not query:
            return
        kb_id = kb_ids[0] if kb_ids else ""
        self._spawn(self.gap_service.record_gap(kb_id, query))

    # ══════════════════════════════════════════════════════════
    # 阻塞模式
    # ══════════════════════════════════════════════════════════

    async def run_general_qa(
        self,
        question: str,
        kb_ids: list[str],
        serial: str | None = None,
        user_id: str = "",
        user_department: str = "",
        enable_query_understanding: bool = True,
    ) -> GeneralQAResponse:
        """通用问答（blocking）：无状态，不落库。

        用调用方流水号或新生成值；出口打一条 [policy-qa] 结果日志供链路追踪
        （只含概要字段，不记录回答正文与引用内容）。
        """
        serial = serial or new_serial()
        set_serial(serial)
        start_ts = time.time()
        model = self.model_cfg["model_name"]

        # Langfuse 埋点：trace_id 用流水号，与 [policy-qa] 日志一致
        langfuse = get_langfuse()
        async with langfuse.start_trace(
            trace_id=serial, session_id=serial, user_id=user_id,
            name="kb_search",
            metadata={
                "mode": "policy-qa", "serial": serial,
                "model": model, "kb_ids": kb_ids,
                "kbId": kb_ids[0] if kb_ids else "",
                "userDepartment": user_department,
                "top_k": REGULATIONS_TOP_K_OVERRIDE,
                "rerank_top_k": REGULATIONS_RERANK_TOP_K_OVERRIDE,
            },
            input={"question": question, "kb_ids": kb_ids},
        ) as trace:
            try:
                final_state = await self._ainvoke_graph(
                    query=question,
                    kb_ids=kb_ids,
                    history=[],
                    conversation_id=serial,  # 无会话，用流水号作 thread_id
                    top_k=REGULATIONS_TOP_K_OVERRIDE,
                    rerank_top_k=REGULATIONS_RERANK_TOP_K_OVERRIDE,
                    enable_query_understanding=enable_query_understanding,
                    user_id=user_id,
                )
                answer = final_state.get("answer", "")
                resources = final_state.get("retrieved_docs", [])
                result_type = (
                    "refused" if final_state.get("refuse")
                    else compute_result_type(answer, len(resources))
                )
                if trace:
                    trace.update(
                        output={
                            "answer": answer,
                            "answer_len": len(answer),
                            "citations": len(resources),
                        },
                        metadata={
                            "resultType": result_type,
                            "citedDocs": build_cited_docs(resources),
                        },
                    )
                langfuse.score(serial, "empty_answer", result_type == "empty")
                self._spawn_gap_record(question, kb_ids, result_type)
            except Exception:
                if trace:
                    trace.update(output={"status": "error"})
                latency_ms = int((time.time() - start_ts) * 1000)
                logger.error(
                    f"[policy-qa] serial={serial} question={question} kb_ids={kb_ids} "
                    f"model={model} status=error "
                    f"answer_len=0 citations=0 latency_ms={latency_ms}"
                )
                raise

        latency_ms = int((time.time() - start_ts) * 1000)
        logger.info(
            f"[policy-qa] serial={serial} question={question} kb_ids={kb_ids} "
            f"model={model} status=completed result_type={result_type} "
            f"answer_len={len(answer)} citations={len(resources)} latency_ms={latency_ms}"
        )

        return GeneralQAResponse(
            answer=answer,
            citations=[RetrieverResource(**r) for r in resources],
            app_serial_number=serial,
            model=model,
            created_at=int(time.time()),
        )

    async def _ainvoke_graph(
        self,
        query: str,
        kb_ids: list[str],
        history: list[dict],
        *,
        conversation_id: str,
        top_k: int = 20,
        rerank_top_k: int = 5,
        score_threshold: float = 0.0,
        thinking_enabled: bool = False,
        enable_query_understanding: bool = True,
        user_id: str = "",
    ) -> dict:
        """构造初始状态并阻塞调用 QA 图（graph.ainvoke + RunnableConfig）。

        RunnableConfig configurable 注入 llm / kb_client / thread_id。
        kb_ids 由调用方决定，本方法不做覆盖。
        """
        thinking_enabled, thinking_mode = resolve_thinking(
            thinking_enabled, self.model_cfg["model_name"],
        )

        request_llm = build_llm(
            self.model_cfg,
            thinking_enabled=thinking_enabled,
            thinking_mode=thinking_mode,
        )

        initial_state: QAState = {
            "query": query,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "history": history,
            "kb_ids": kb_ids,
            "kb_names": [],
            "top_k": top_k,
            "rerank_top_k": rerank_top_k,
            "score_threshold": score_threshold,
            "enable_query_understanding": enable_query_understanding,
            "standalone_query": "",
            "retrieved_docs": [],
            "answer": "",
            "error": None,
        }

        config: RunnableConfig = {
            "configurable": {
                "llm": request_llm,
                "kb_client": self.kb_client,
                "thread_id": conversation_id,
            }
        }

        return await self.graph.ainvoke(initial_state, config=config)

    # ══════════════════════════════════════════════════════════
    # 流式模式
    # ══════════════════════════════════════════════════════════

    async def run_general_qa_stream(
        self,
        question: str,
        kb_ids: list[str],
        serial: str | None = None,
        user_id: str = "",
        user_department: str = "",
        enable_query_understanding: bool = True,
    ) -> StreamingResponse:
        """通用问答（response_mode=streaming）：无状态 SSE 流式，不落库。

        与 run_general_qa（blocking）同一套输入契约，差别仅在输出：
        - 复用 _map_event_to_sse 做事件映射，SSE 事件契约对齐
          （message_start → stage → citation → content_block_delta → message_delta
          → message_stop），无 title_generated / suggestions 等对话态事件。
        - thinking 关闭（与 blocking 一致，不新增思考模式）。
        - 出口打一条 [policy-qa] 概要日志（含 serial/mode/status/latency，不记正文），
          空应答后台落知识缺口工单，正文与引用写 Langfuse trace output 供溯源。
        """
        serial = serial or new_serial()
        set_serial(serial)
        start_ts = time.time()
        model = self.model_cfg["model_name"]

        thinking_enabled, thinking_mode = resolve_thinking(False, model)
        request_llm = build_llm(
            self.model_cfg,
            thinking_enabled=thinking_enabled,
            thinking_mode=thinking_mode,
        )

        initial_state: QAState = {
            "query": question,
            "user_id": user_id,
            "conversation_id": serial,  # 无会话，用流水号作 thread_id
            "history": [],
            "kb_ids": kb_ids,
            "kb_names": [],
            "top_k": REGULATIONS_TOP_K_OVERRIDE,
            "rerank_top_k": REGULATIONS_RERANK_TOP_K_OVERRIDE,
            "score_threshold": 0.0,
            "enable_query_understanding": enable_query_understanding,
            "standalone_query": "",
            "retrieved_docs": [],
            "answer": "",
            "error": None,
        }

        config: RunnableConfig = {
            "configurable": {
                "llm": request_llm,
                "kb_client": self.kb_client,
                "thread_id": serial,
            }
        }

        langfuse = get_langfuse()

        async def _stream_body(trace) -> AsyncGenerator[str, None]:
            collected_answer = ""
            collected_resources: list[dict] = []
            tokens = {"prompt": 0, "completion": 0}
            stage_states: dict[str, str] = {}
            thinking_active: dict[str, bool] = {"active": False}
            thinking_list: list[str] = []
            content_streamed: dict[str, bool] = {"v": False}
            final_status = "completed"
            stop_reason = "end_turn"
            final_refuse = False

            local_tasks: list[asyncio.Task] = []
            # 心跳保活：与 graph 事件竞争，避免长空闲被网关掐断（通用问答无 stop 端点）
            control_queue: asyncio.Queue[str] = asyncio.Queue()

            async def _control_loop() -> None:
                while True:
                    await asyncio.sleep(15)
                    await control_queue.put("ping")

            ev_task: asyncio.Task | None = None
            ctl_task: asyncio.Task | None = None

            try:
                yield self._sse("message_start", {
                    "type": "message_start",
                    "message_id": serial,
                    "conversation_id": serial,
                    "model": model,
                    "timestamp": int(time.time()),
                })

                control_task = self._spawn(_control_loop())
                local_tasks.append(control_task)

                stream = self.graph.astream_events(
                    initial_state, config=config, version="v2",
                )
                stream_iter = stream.__aiter__()
                # ev_task 跨循环复用：心跳 ping 绝不能取消它，否则 CancelledError
                # 会穿透进 LangGraph 正在执行的节点，把 graph 中断（表现为某个
                # stage started 后无下文、无输出、无报错）。
                ev_task = asyncio.ensure_future(anext(stream_iter))
                while True:
                    ctl_task = asyncio.ensure_future(control_queue.get())
                    done, _ = await asyncio.wait(
                        {ev_task, ctl_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if ev_task in done:
                        ctl_task.cancel()
                        await asyncio.gather(ctl_task, return_exceptions=True)
                        try:
                            event = ev_task.result()
                        except StopAsyncIteration:
                            break
                        for line in self._map_event_to_sse(
                            event, stage_states, collected_resources, tokens,
                            thinking_active, thinking_list,
                            thinking_enabled, content_streamed,
                        ):
                            yield line
                        ev_task = asyncio.ensure_future(anext(stream_iter))
                    else:
                        # 心跳先到：仅保活，ev_task 仍在等待，不能取消
                        yield self._sse("ping", {"type": "ping"})

                await stream.aclose()

                # graph 完成 → 获取最终状态
                final_state = self.graph.get_state(config)
                if final_state and final_state.values:
                    collected_answer = final_state.values.get("answer", "")
                    collected_resources = final_state.values.get("retrieved_docs", [])
                    final_refuse = bool(final_state.values.get("refuse"))

                # ── 补发未流式输出的最终答案 ──
                # 空检索时 generate 短路（不调 LLM），answer 只写进 graph state，
                # 不会产生 on_chat_model_stream → 调用方收不到正文，这里补发。
                if collected_answer and not content_streamed["v"]:
                    yield self._sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"text": collected_answer},
                    })

                yield self._sse("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason},
                    "usage": {
                        "prompt_tokens": tokens["prompt"],
                        "completion_tokens": tokens["completion"],
                        "total_tokens": tokens["prompt"] + tokens["completion"],
                        "latency": round(time.time() - start_ts, 2),
                    },
                })

            except asyncio.CancelledError:
                # 客户端断开
                logger.info(f"[policy-qa] serial={serial} client disconnected")
                final_status = "stopped"
                stop_reason = "stop"
            except Exception as exc:
                logger.exception(f"[policy-qa] serial={serial} question={question} failed: {exc}")
                final_status = "error"
                stop_reason = "error"
                # 区分 LLM 错误 vs 其他错误（顺序：先判检索（消息含知识库/检索等
                # 词，也可能含 connection），再判 LLM（连接/网络类错误））
                err_msg = str(exc)
                err_code = "internal_server_error"
                if any(kw in err_msg.lower() for kw in ("retrieval", "kb", "knowledge", "知识库", "检索")):
                    err_code = "retrieval_error"
                elif any(kw in err_msg.lower() for kw in (
                    "openai", "model", "chat", "completion", "api key", "timeout",
                    "rate limit", "connection", "connect", "network", "unreachable",
                    "refused", "getaddrinfo", "resolve", "dns", "ssl",
                )):
                    err_code = "completion_request_error"
                yield self._sse("error", {
                    "type": "error",
                    "code": err_code,
                    "message": "模型服务暂时不可用" if err_code == "completion_request_error" else str(exc),
                    "timestamp": int(time.time()),
                })
            finally:
                # 清理：取消控制任务/未完成的 graph 拉取
                for t in local_tasks:
                    t.cancel()
                if ev_task and not ev_task.done():
                    ev_task.cancel()
                if ctl_task and not ctl_task.done():
                    ctl_task.cancel()
                await asyncio.gather(*local_tasks, return_exceptions=True)
                await asyncio.gather(
                    *[t for t in (ev_task, ctl_task) if t and not t.done()],
                    return_exceptions=True,
                )

                # ── message_stop ──（断连 aclose() 时不能再 yield，忽略）
                try:
                    yield self._sse("message_stop", {"type": "message_stop"})
                except (RuntimeError, GeneratorExit):
                    pass

                # ── 写 Langfuse 输出（trace 由外层 async with 自动 end）──
                result_type = (
                    "refused" if final_refuse
                    else compute_result_type(collected_answer, len(collected_resources))
                )
                if trace:
                    trace.update(
                        output={
                            "answer": collected_answer,
                            "answer_len": len(collected_answer),
                            "citations": len(collected_resources),
                        },
                        metadata={
                            "resultType": result_type,
                            "citedDocs": build_cited_docs(collected_resources),
                        },
                    )
                langfuse.score(serial, "empty_answer", result_type == "empty")
                self._spawn_gap_record(question, kb_ids, result_type)

                # ── [policy-qa] 概要日志（不记正文，正文去 Langfuse 查）──
                latency_ms = int((time.time() - start_ts) * 1000)
                logger.info(
                    f"[policy-qa] serial={serial} question={question} kb_ids={kb_ids} "
                    f"model={model} status={final_status} mode=streaming "
                    f"result_type={result_type} "
                    f"answer_len={len(collected_answer)} citations={len(collected_resources)} "
                    f"latency_ms={latency_ms}"
                )

        async def event_stream() -> AsyncGenerator[str, None]:
            async with langfuse.start_trace(
                trace_id=serial, session_id=serial, user_id=user_id,
                name="kb_search",
                metadata={
                    "mode": "policy-qa-streaming", "serial": serial,
                    "model": model, "kb_ids": kb_ids,
                    "kbId": kb_ids[0] if kb_ids else "",
                    "userDepartment": user_department,
                    "top_k": REGULATIONS_TOP_K_OVERRIDE,
                    "rerank_top_k": REGULATIONS_RERANK_TOP_K_OVERRIDE,
                },
                input={"question": question, "kb_ids": kb_ids},
            ) as trace:
                async for ev in _stream_body(trace):
                    yield ev

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ══════════════════════════════════════════════════════════
    # 事件映射
    # ══════════════════════════════════════════════════════════

    def _map_event_to_sse(
        self,
        event: dict,
        stage_states: dict[str, str],
        collected_resources: list[dict],
        tokens: dict[str, int],
        thinking_active: dict[str, bool],
        collected_thinking: list[str],
        thinking_enabled: bool,
        content_streamed: dict[str, bool],
    ):
        """将 LangGraph 运行时事件转换为 SSE 字符串列表。

        thinking_block_delta / thinking_end 分支保留（thinking 当前恒关闭，
        防模型仍返回 reasoning 时事件契约缺失）。
        """
        kind = event.get("event", "")
        name = event.get("name", "")
        event_data = event.get("data") or {}
        node_input = event_data.get("input", {})
        # 从 on_chat_model_end 提取 token 用量
        if kind == "on_chat_model_end":
            output = event_data.get("output", {})
            if hasattr(output, "usage_metadata") and output.usage_metadata:
                um = output.usage_metadata
                tokens["prompt"] += um.get("input_tokens", 0)
                tokens["completion"] += um.get("output_tokens", 0)
            elif isinstance(output, dict):
                um = output.get("usage_metadata") or {}
                tokens["prompt"] += um.get("input_tokens", 0)
                tokens["completion"] += um.get("output_tokens", 0)

        # ── 节点开始 → stage started ──
        if kind == "on_chain_start" and name in ("rewrite", "retrieve", "generate"):
            # 只发一次 started，避免重复
            if stage_states.get(name) == "started":
                return
            stage_states[name] = "started"

            labels = {
                "rewrite":   "正在理解问题...",
                "retrieve":  "正在检索知识库...",
                "generate":  "正在整理回答内容...",
            }
            payload: dict = {
                "type": "stage",
                "name": name,
                "status": "started",
                "label": labels.get(name, ""),
                "timestamp": int(time.time()),
            }
            # retrieve 阶段附 KB 信息（kb_names 在检索完成后从结果中提取）
            if name == "retrieve":
                payload["kb_ids"] = node_input.get("kb_ids", [])
                payload["kb_names"] = []
            yield self._sse("stage", payload)

        # ── 节点结束 → stage completed ──
        if kind == "on_chain_end" and name in ("rewrite", "retrieve", "generate"):
            stage_states[name] = "completed"
            yield self._sse("stage", {
                "type": "stage",
                "name": name,
                "status": "completed",
                "timestamp": int(time.time()),
            })

            # retrieve 结束后 → citation 事件
            if name == "retrieve":
                docs = (event.get("data") or {}).get("output", {}).get("retrieved_docs", [])
                if docs:
                    collected_resources[:] = docs
                    # 从检索结果中提取 kb_names
                    kb_names = list({r.get("dataset_name", "") for r in docs if r.get("dataset_name")})
                    yield self._sse("citation", {
                        "type": "citation",
                        "resources": docs,
                    })
                    # 补发一个 stage 事件带上 kb_names
                    if kb_names:
                        yield self._sse("stage", {
                            "type": "stage",
                            "name": "retrieve",
                            "status": "completed",
                            "label": f"检索完成: {', '.join(kb_names)}",
                            "kb_ids": node_input.get("kb_ids", []),
                            "kb_names": kb_names,
                            "timestamp": int(time.time()),
                        })

        # ── LLM token 流（只透传 generate 节点的输出）──
        if kind == "on_chat_model_stream":
            # rewrite 等节点调 LLM 时也会触发 on_chat_model_stream（langchain 内部
            # 用 _astream 聚合），但那些输出不能当正文发给前端，只放行 generate 节点。
            # 否则前端会看到"把问题重复一遍输出"。
            if (event.get("metadata") or {}).get("langgraph_node") != "generate":
                return
            chunk = (event.get("data") or {}).get("chunk")
            if chunk is None:
                return

            # ── thinking 内容 ──
            reasoning = ""
            if hasattr(chunk, "additional_kwargs") and chunk.additional_kwargs:
                reasoning = chunk.additional_kwargs.get("reasoning_content", "") or ""

            if reasoning and thinking_enabled:
                thinking_active["active"] = True
                collected_thinking.append(reasoning)
                yield self._sse("thinking_block_delta", {
                    "type": "thinking_block_delta",
                    "index": 0,
                    "delta": {"thinking": reasoning},
                })

            # ── thinking_end 发射时机：thinking 激活后首次收到正文 token ──
            content_text = ""
            if hasattr(chunk, "content") and chunk.content:
                content_text = chunk.content

            if content_text and thinking_active.get("active") and not reasoning:
                if thinking_enabled:
                    yield self._sse("thinking_end", {"type": "thinking_end"})
                thinking_active["active"] = False

            # ── 正文内容 ──
            if content_text:
                content_streamed["v"] = True
                yield self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"text": content_text},
                })

        # ── 心跳 (generation 阶段空闲时) ──
        # 由外部定时器处理（见 run_general_qa_stream 中的 _control_loop）

    # ══════════════════════════════════════════════════════════
    # 内部辅助
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _sse(event_type: str, data: dict) -> str:
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
