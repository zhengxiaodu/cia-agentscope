"""聊天服务：模型创建 + 编排服务适配层。

重构后的职责：
- 保留 create_model_from_config / load_model_config（被 orchestrator_service 依赖）
- generate_response 作为适配层：转调 OrchestratorService.run()，
  并集成 session 历史持久化、Langfuse 追踪、CUSTOM_COMPONENT 事件检测

多智能体编排的核心逻辑下沉到 app.services.orchestrator_service。
"""
import json
import logging
import os
import asyncio
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, Iterator, List, Optional, Tuple

import yaml
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel

from app.config import MODEL_CONFIG_PATH
from app.services.file_change_detector import diff
from app.services.langfuse_service import LangfuseService
from app.services.sensitive_service import (
    build_message_replace_event,
    check_sensitive,
    dict_check_sensitive,
)
from app.intent.llm_client import chat_complete, extract_json

logger = logging.getLogger(__name__)


@contextmanager
def _noop_ctx() -> Iterator[None]:
    """空 context manager，langfuse 未启用时作为 span 占位，yield None。"""
    yield None


def _estimate_tokens(content: str) -> int:
    """按内容长度估算 token 数（deepseek-v4，中英文分别计系数，粗略估算）。

    经验值：中文 1 字 ≈ 1.5 token，英文 4 字符 ≈ 1 token（0.25/字符）。
    """
    if not content:
        return 0
    chinese_count = sum(1 for c in content if ord(c) > 127)
    ascii_count = len(content) - chinese_count
    return int(chinese_count * 1.5 + ascii_count * 0.25)


def _compute_ttft_marker(event_type: str) -> bool:
    """判断某事件类型是否是首个用户可见内容事件（用于 TTFT 计时）。"""
    return event_type in ("TEXT_BLOCK_DELTA", "summary")


async def _generate_recommended_questions(
    orchestrator_service,
    user_input: str,
    final_output: str,
) -> Tuple[List[str], dict]:
    """调用 LLM 根据本轮问答生成 3 个推荐问题。

    复用 orchestrator 的 _intent_client / _intent_model_cfg。
    任何异常都吞掉返回空列表，确保不影响主流程。

    Returns:
        (推荐问题列表, {"system_prompt": ..., "user_prompt": ...})；
        失败时返回 ([], {})。
    """
    try:
        client = getattr(orchestrator_service, "_intent_client", None)
        model_config = getattr(orchestrator_service, "_intent_model_cfg", None)
        if not client or not model_config or not final_output:
            return [], {}

        system_prompt = (
            "你是一个推荐问题生成助手。根据用户的提问和助手的回答，"
            "生成 3 个用户可能想继续追问的相关问题。"
            "只输出 JSON，格式为 {\"questions\": [\"问题1\", \"问题2\", \"问题3\"]}，"
            "不要有任何额外说明或 markdown 标记。"
        )
        user_prompt = f"用户提问：{user_input}\n\n助手回答：{final_output}"

        text = await chat_complete(client, model_config, system_prompt, user_prompt, stage="llm-recommended-questions")
        data = extract_json(text)
        if not isinstance(data, dict):
            return [], {}
        questions = data.get("questions", [])
        if not isinstance(questions, list):
            return [], {}
        # 最多 3 个，过滤空白与非字符串
        cleaned = [str(q).strip() for q in questions if q and str(q).strip()]
        return cleaned[:3], {"system_prompt": system_prompt, "user_prompt": user_prompt}
    except Exception:
        logger.debug("[chat_service] 生成推荐问题失败", exc_info=True)
        return [], {}


def _extract_user_input(messages: List[Dict[str, Any]]) -> str:
    """从 messages 提取最后一条 user 消息的文本（支持 str / content block 列表）。

    统一复用，消除推荐问题与持久化两处重复提取逻辑。
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                return "\n".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            return str(content)
    return ""


def _safe_update_span(span: Any, output_dict: dict) -> None:
    """安全更新 span 的 output 字段，span 为 None 或更新异常均静默忽略。"""
    if not span:
        return
    try:
        span.update(output=output_dict)
    except Exception:
        pass


async def _load_history_messages(session_service: Any, session_id: Optional[str]) -> List[dict]:
    """从 session 加载历史消息并截断为最近 3 轮（6 条）。

    加载失败返回空列表，不影响主流程。
    """
    if not (session_service and session_id):
        return []
    try:
        saved = await session_service.load_messages(session_id)
        history = [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in saved
            if m.get("content")
        ]
        # 限制历史为最近 3 轮（6 条消息），避免超出模型 20K token 上下文
        return history[-6:]
    except Exception:
        logger.exception("[chat_service] 加载会话历史失败")
        return []


async def _emit_recommended_questions(
    orchestrator_service,
    messages: List[Dict[str, Any]],
    final_output: str,
    langfuse_service: LangfuseService,
) -> AsyncGenerator[str, None]:
    """编排流结束后立即生成推荐问题并 yield recommended_questions 事件。

    生成失败静默跳过，不影响后续持久化与 trace 收尾。
    """
    try:
        user_input = _extract_user_input(messages)

        root_span_ctx = (
            langfuse_service.start_span(
                "recommended-questions",
                input={"user_input": user_input, "reply": final_output},
            )
            if langfuse_service and langfuse_service.enabled
            else _noop_ctx()
        )
        with root_span_ctx as rec_obs:
            questions, rq_prompts = await _generate_recommended_questions(
                orchestrator_service, user_input, final_output
            )
            _safe_update_span(rec_obs, {"questions": questions, "prompts": rq_prompts})

        rec_event = json.dumps(
            {"type": "recommended_questions", "questions": questions},
            ensure_ascii=False,
        )
        yield f"data: {rec_event}\n\n"
    except Exception:
        logger.debug("[chat_service] 推荐问题事件发送失败", exc_info=True)


async def _persist_conversation_history(
    orchestrator_service,
    session_service: Any,
    session_id: Optional[str],
    user_id: Optional[str],
    messages: List[Dict[str, Any]],
    final_output: str,
    upload_file_dao: Any = None,
) -> None:
    """持久化本轮对话历史（用户输入 + 智能体输出）。

    从 orchestrator_service 提取本轮参与的 agent_id 列表与成功标志，
    与 user/assistant 消息一同写入 messages 表。任何异常均静默吞掉。
    落库成功后把本轮 user 消息 id 回填到该会话未绑定的上传文件
    （upload_files.message_id），避免下一轮重复注入解析内容。
    """
    if not (session_service and session_id and user_id):
        missing = []
        if not session_service:
            missing.append("session_service")
        if not session_id:
            missing.append("session_id")
        if not user_id:
            missing.append("user_id")
        logger.warning(f"[chat_service] 跳过持久化：{', '.join(missing)} 为空")
        return

    user_message_id = None
    try:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        user_input = _extract_user_input(messages)

        new_messages = []
        if user_input:
            new_messages.append({
                "role": "user", "content": user_input, "timestamp": now_str,
                "agent_ids": [],
                "user_id": user_id,
                "success": True,
                "tokens": _estimate_tokens(user_input),
            })
        if final_output:
            # 取本轮参与的 agent_id 列表（单 agent 路径=[agent_id]，多 agent 路径=编排汇总）
            involved_agent_ids = []
            if orchestrator_service is not None:
                try:
                    involved_agent_ids = orchestrator_service.last_agent_ids
                except Exception:
                    involved_agent_ids = []
            # 取本轮编排是否成功（基于 TaskResult.success，异常/超时/失败为 False）
            last_success = True
            if orchestrator_service is not None:
                try:
                    last_success = orchestrator_service.last_success
                except Exception:
                    last_success = False
            new_messages.append({
                "role": "assistant", "content": final_output, "timestamp": now_str,
                "agent_ids": involved_agent_ids,
                "user_id": user_id,
                "success": last_success,
                "tokens": _estimate_tokens(final_output),
            })
        if new_messages:
            user_message_id = await session_service.append_messages(
                session_id, user_id, new_messages
            )
    except Exception:
        logger.exception("[chat_service] 持久化对话历史失败")

    # 回填上传文件的 message_id（失败仅告警，不影响主流程）
    if user_message_id and upload_file_dao:
        try:
            bound = await upload_file_dao.bind_message_id(session_id, user_message_id)
            if bound:
                logger.info(
                    "[chat_service] 已绑定 %s 个上传文件到消息 %s",
                    bound, user_message_id,
                )
        except Exception:
            logger.warning("[chat_service] 回填上传文件 message_id 失败", exc_info=True)


async def _detect_and_emit_files(
    session_id: Optional[str],
    before_files: set,
    session_service: Any,
    workspace_manager: Any = None,
    user_id: str = "",
) -> AsyncGenerator[str, None]:
    """检测本轮新文件并 yield files_generated 事件，随后持久化文件元信息。

    通过 workspace_manager.list_session_files/stat_session_file 读取沙箱内
    会话目录的文件列表与大小。
    """
    import mimetypes

    files_payload = []
    try:
        if workspace_manager is not None and session_id:
            after_files = await workspace_manager.list_session_files(user_id, session_id)
            new_files = diff(before_files, after_files)
            for rel_path in new_files:
                size = await workspace_manager.stat_session_file(user_id, session_id, rel_path)
                if size is None:
                    continue
                media_type = mimetypes.guess_type(rel_path)[0] or "application/octet-stream"
                files_payload.append({
                    "name": os.path.basename(rel_path),
                    "path": rel_path,
                    "url": f"/files/{session_id}/{rel_path}",
                    "size": size,
                    "media_type": media_type,
                })
    except Exception:
        logger.warning("[chat_service] 检测新文件失败", exc_info=True)
    yield f"data: {json.dumps({'type': 'files_generated', 'files': files_payload}, ensure_ascii=False)}\n\n"

    # 持久化本轮生成的文件元信息（供 /sessions/{session_id} 回看）
    if files_payload and session_service and session_id:
        try:
            await session_service.append_session_files(session_id, files_payload)
        except Exception:
            logger.warning("[chat_service] 持久化生成文件元信息失败", exc_info=True)


async def _finalize_trace(
    langfuse_service: LangfuseService,
    root_obs: Any,
    session_service: Any,
    session_id: Optional[str],
    already_emitted: bool = False,
) -> AsyncGenerator[str, None]:
    """Langfuse trace 收尾：flush + （可选）yield trace_ready + 保存 trace_id。

    在根 span context manager 之外执行，保证 span 已 end。
    already_emitted=True 时跳过 trace_ready 下发与 trace_id 保存（已在流首处理），
    但仍执行 flush。
    """
    if langfuse_service and langfuse_service.enabled:
        try:
            langfuse_service.flush()
        except Exception:
            pass

    if not already_emitted:
        trace_id = None
        try:
            trace_id = root_obs.trace_id if root_obs else None
        except Exception:
            pass
        trace_event = json.dumps({"type": "trace_ready", "trace_id": trace_id})
        yield f"data: {trace_event}\n\n"

        # 保存 trace_id 到 Redis 元信息
        if session_service and session_id and trace_id:
            await session_service.save_latest_trace_id(session_id, trace_id)


def load_model_config(config_path: str = MODEL_CONFIG_PATH) -> dict:
    """加载模型配置"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def create_model_from_config(model_config: dict):
    """根据配置创建模型实例（业务智能体流式对话用）。

    被 AgentRegistry 的 create_model_fn 调用，每次返回新实例。
    """
    provider = model_config.get("provider", "openai")
    base_url = model_config.get("base_url", "https://api.deepseek.com/v1")
    model_name = model_config.get("model_name", "deepseek-chat")
    api_key = model_config.get("api_key", "OPENAI_API_KEY")
    parameters = model_config.get("parameters", {})

    if not api_key:
        raise ValueError("环境变量未设置")

    if provider == "openai":
        credential = OpenAICredential(api_key=api_key, base_url=base_url)
        # context_size 按 max_tokens * 0.8 赋值，为输出预留余量
        max_tokens = parameters.get("max_tokens", 0)
        context_size = int(max_tokens * 0.8) if max_tokens else None
        model = OpenAIChatModel(
            credential=credential,
            model=model_name,
            stream=True,
            parameters=OpenAIChatModel.Parameters(**parameters),
            context_size=context_size,
            # vLLM 部署的 Qwen3 等模型默认开启 thinking，显式关闭，
            # 通过 extra_body 透传 chat_template_kwargs 到请求体
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    else:
        raise ValueError(f"不支持的 provider: {provider}")

    return model


# 需要拦截并转发为 CUSTOM_COMPONENT 事件的组件类型
_COMPONENT_TYPES = {"chart", "volume_chart", "selectable_list", "confirm_action"}


def _extract_components_from_delta(delta: str):
    """从 TOOL_RESULT_TEXT_DELTA 的 delta 文本中提取组件 JSON。

    沿用原有逻辑：通过花括号匹配提取嵌套 JSON 对象，
    若 type 属于 _COMPONENT_TYPES 则生成 CUSTOM_COMPONENT 事件。
    """
    if not delta:
        return
    i = 0
    while i < len(delta):
        if delta[i] == "{":
            depth = 1
            j = i + 1
            while j < len(delta) and depth > 0:
                if delta[j] == "{":
                    depth += 1
                elif delta[j] == "}":
                    depth -= 1
                j += 1
            if depth == 0:
                try:
                    data = json.loads(delta[i:j])
                    if isinstance(data, dict) and data.get("type") in _COMPONENT_TYPES:
                        yield {
                            "type": "CUSTOM_COMPONENT",
                            "component": data,
                        }
                except json.JSONDecodeError:
                    pass
                i = j
            else:
                i += 1
        else:
            i += 1


async def generate_response(
    orchestrator_service,
    messages: List[Dict[str, Any]],
    session_id: str = None,
    user_id: str = None,
    session_service=None,
    langfuse_service: LangfuseService = None,
    agent_id: Optional[str] = None,
    request=None,
    search_enabled: bool = True,
    skills: List[str] = None,
    cancel_event: Optional[asyncio.Event] = None,
    workspace_manager: Any = None,
) -> AsyncGenerator[str, None]:
    """根据消息列表生成流式回复（多智能体编排版本）。

    主流程：加载历史 → 快照 → 根span → 输入敏感检测 → 执行编排 → 输出敏感检测 →
    推荐问题 → 持久化 → 文件检测 → trace收尾。
    中断处理：cancel_event 被 set 时主动 raise CancelledError，进入 finally 保证
    落库（success=False）+ flush trace + yield user_abort 事件。
    敏感拦截：输入为纯词典检测（在根 span 内执行，span 嵌套在本轮会话 trace 中），
    命中时 yield message_replace 事件并结束本轮、不持久化；final_output 命中敏感时
    yield message_replace 事件并跳过推荐问题，持久化与文件检测仍执行。
    """
    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # ① 加载历史（限 3 轮）并合并当前输入
    history_messages = await _load_history_messages(session_service, session_id)
    full_messages = history_messages + messages
    final_output_parts: List[str] = []
    # 兜底输出：当无 summary 时（如 pipeline 失败终止、react 无 action 终止），
    # 用 pipeline_intercept.message / react_final.conclusion 作为 final_output，
    # 保证失败轮次也能持久化 assistant 消息
    final_fallback_parts: List[str] = []
    final_output = ""
    aborted = False
    t0 = time.perf_counter()
    ttft_ms = None
    aborted_at_ms = None

    # ② 快照工作目录（用于结束后检测新文件）
    before_files: set = set()
    if session_id:
        try:
            if workspace_manager is not None:
                before_files = await workspace_manager.list_session_files(user_id, session_id)
        except Exception:
            logger.warning("[chat_service] 快照 session 工作目录失败", exc_info=True)

    # ③ 根 span 包裹主体（保持活跃使子 span 自动嵌套）
    root_span_ctx = (
        langfuse_service.start_span(
            "chat-response",
            input={
                "messages": full_messages,
                "session_id": session_id,
                "user_id": user_id,
            },
        )
        if langfuse_service and langfuse_service.enabled
        else _noop_ctx()
    )
    root_obs = None
    trace_emitted = False
    try:
        with root_span_ctx as root_obs:
            # P0-3: 流首即下发真实 trace_id（span 内即可读），前端可立即关联反馈
            trace_id = getattr(root_obs, "trace_id", None) if root_obs else None
            if trace_id:
                trace_emitted = True
                yield f"data: {json.dumps({'type': 'trace_ready', 'trace_id': trace_id})}\n\n"
                # 流首保存 trace_id（替代原 finalize 阶段保存，供反馈关联）
                if session_service and session_id:
                    try:
                        await session_service.save_latest_trace_id(session_id, trace_id)
                    except Exception:
                        logger.warning("[chat_service] 流首保存 trace_id 失败", exc_info=True)
            # ③.5 用户输入敏感检测（纯词典，低延迟）。
            # 置于根 span 内执行，使 sensitive-dict-check span 嵌套在本轮会话
            # trace 中（而非形成独立 trace）；命中则发 message_replace 并结束本轮，
            # 不进入编排主流程，也不持久化消息
            sens_input = await dict_check_sensitive(
                _extract_user_input(messages), stage="input"
            )
            if sens_input["blocked"]:
                replace_event = build_message_replace_event(sens_input, stage="input")
                yield f"data: {json.dumps(replace_event, ensure_ascii=False)}\n\n"
                _safe_update_span(root_obs, {
                    "reply": "",
                    "session_id": session_id,
                    "user_id": user_id,
                    "sensitiveBlocked": True,
                    "blockedStage": "input",
                })
                return

            # ④ 执行编排主流程，实时透传 SSE 事件
            async for event_str in orchestrator_service.run(
                full_messages,
                session_id=session_id,
                user_id=user_id,
                session_service=session_service,
                agent_id=agent_id,
                request=request,
                search_enabled=search_enabled,
                skills=skills or [],
                langfuse_service=langfuse_service,
            ):
                # 取消检查点：每个事件之间检测一次（LLM chunk 之间会回到本循环）
                if _cancelled():
                    aborted = True
                    aborted_at_ms = int((time.perf_counter() - t0) * 1000)
                    raise asyncio.CancelledError()
                yield event_str

                # 解析事件：提取 summary 收集输出 + 检测 CUSTOM_COMPONENT 并转发
                try:
                    if event_str.startswith("data: ") and event_str.endswith("\n\n"):
                        payload = json.loads(event_str[6:].strip())
                        event_type = payload.get("type", "")

                        if ttft_ms is None and _compute_ttft_marker(event_type):
                            ttft_ms = int((time.perf_counter() - t0) * 1000)

                        if event_type == "summary":
                            final_output_parts.append(payload.get("content", ""))

                        # 兜底收集失败终止事件的文本（仅当无 summary 时才启用）
                        if event_type == "pipeline_intercept":
                            final_fallback_parts.append(payload.get("message", ""))
                        elif event_type == "react_final":
                            final_fallback_parts.append(payload.get("conclusion", ""))

                        if event_type == "TOOL_RESULT_TEXT_DELTA":
                            delta = payload.get("delta", "")
                            for component in _extract_components_from_delta(delta):
                                yield f"data: {json.dumps(component, ensure_ascii=False)}\n\n"
                except Exception:
                    logger.debug("[chat_service] 事件解析跳过", exc_info=True)

            final_output = "\n".join(final_output_parts).strip()
            # 无 summary 时用失败终止事件文本兜底，保证失败轮次也有 assistant 记录
            if not final_output:
                final_output = "\n".join(p for p in final_fallback_parts if p).strip()

            # 编排流结束后的取消检查（避免后续步骤继续执行）
            if _cancelled():
                aborted = True
                aborted_at_ms = int((time.perf_counter() - t0) * 1000)
                raise asyncio.CancelledError()

            # 安全敏感检测：编排输出 final_output（推荐问题生成之前）
            # 命中则发送 message_replace 事件并跳过推荐问题（停止后续流式输出）；
            # 持久化与文件检测仍执行，保证会话数据完整；
            # 服务未配置或异常时兜底放行，不影响主流程
            output_blocked = False
            if final_output:
                sens_result = await check_sensitive(final_output, stage="output")
                if sens_result["blocked"]:
                    output_blocked = True
                    replace_event = build_message_replace_event(sens_result, stage="output")
                    yield f"data: {json.dumps(replace_event, ensure_ascii=False)}\n\n"

            # ⑤ 编排流结束立即生成推荐问题（前置，让前端尽快拿到）
            if not output_blocked:
                async for ev in _emit_recommended_questions(
                    orchestrator_service, messages, final_output, langfuse_service,
                ):
                    yield ev

            # ⑥ 持久化对话历史（用户输入 + 智能体输出）+ 回填上传文件 message_id
            await _persist_conversation_history(
                orchestrator_service, session_service, session_id, user_id,
                messages, final_output,
                upload_file_dao=getattr(
                    request.app.state, "upload_file_dao", None
                ) if request is not None else None,
            )

            # ⑦ 检测本轮新文件
            async for ev in _detect_and_emit_files(
                session_id, before_files, session_service,
                workspace_manager=workspace_manager,
                user_id=user_id or "",
            ):
                yield ev

            # 更新根 span 输出（context manager 退出时自动 end）
            _safe_update_span(root_obs, {
                "reply": final_output,
                "session_id": session_id,
                "user_id": user_id,
                "ttftMs": ttft_ms,
                "totalMs": int((time.perf_counter() - t0) * 1000),
                "aborted": aborted,
                "abortedAtMs": aborted_at_ms,
                "sensitiveBlocked": output_blocked,
            })
    except asyncio.CancelledError:
        # 中断时吞掉 CancelledError，进入 finally 做清理（不重新 raise，让流优雅结束）
        aborted = True
        if aborted_at_ms is None:
            aborted_at_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(f"[chat_service] 会话被用户中断 session={session_id}")
    finally:
        # 中断时也落库：success 强制 False（绕开单例 last_success 污染）
        if aborted:
            if not final_output:
                final_output = "\n".join(p for p in final_fallback_parts if p).strip()
            await _persist_conversation_history(
                orchestrator_service, session_service, session_id, user_id,
                messages, final_output,
            )
            # _persist 内部读 orchestrator_service.last_success（单例，不可信），补 UPDATE 强制失败
            if session_service and session_id and user_id:
                try:
                    await session_service.mark_last_assistant_failed(session_id, user_id)
                except Exception:
                    logger.warning("[chat_service] 标记中断消息失败状态失败", exc_info=True)

        # ⑧ trace 收尾（保证 span 已 end + flush）
        async for ev in _finalize_trace(
            langfuse_service, root_obs, session_service, session_id,
            already_emitted=trace_emitted,
        ):
            yield ev

        # 中断时给前端发送 user_abort 事件
        if aborted:
            abort_event = {"type": "user_abort", "session_id": session_id, "message": "用户已停止"}
            yield f"data: {json.dumps(abort_event, ensure_ascii=False)}\n\n"
