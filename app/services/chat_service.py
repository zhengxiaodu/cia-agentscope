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
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List,Optional

import yaml
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel

from app.config import MODEL_CONFIG_PATH, WORKSPACE_BASEDIR
from app.services.file_change_detector import snapshot, diff, build_file_meta
from app.services.langfuse_service import LangfuseService
from fastapi import HTTPException

logger = logging.getLogger(__name__)


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
        model = OpenAIChatModel(
            credential=credential,
            model=model_name,
            stream=True,
            parameters=OpenAIChatModel.Parameters(**parameters),
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
) -> AsyncGenerator[str, None]:
    """根据消息列表生成流式回复（多智能体编排版本）。

    流程：
    1. 从 session 加载历史上下文 → 拼入 messages
    2. 调用 OrchestratorService.run() 执行改写→识别→编排
    3. 透传 SSE 事件，检测 CUSTOM_COMPONENT
    4. 流结束后持久化对话历史 + Langfuse 追踪
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not set")

    # 从 session 加载历史消息，拼接到 messages 前面作为上下文
    history_messages = []
    if session_service and session_id:
        try:
            saved = await session_service.load_messages(session_id)
            history_messages = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in saved
                if m.get("content")
            ]
        except Exception:
            logger.exception("[chat_service] 加载会话历史失败")

    # 合并：历史 + 当前请求（当前请求的最后一条是本次用户输入）
    full_messages = history_messages + messages

    # 创建 Langfuse observation
    obs = None
    if langfuse_service and langfuse_service.enabled:
        obs = langfuse_service.start_observation(
            name="chat-response",
            as_type="span",
            input={
                "messages": full_messages,
                "session_id": session_id,
                "user_id": user_id,
            },
        )

    # 收集最终输出（用于持久化和 langfuse）
    final_output_parts: List[str] = []

    # ---- 本轮开始前快照 session 工作目录（用于结束后检测新文件） ----
    before_files: set = set()
    if session_id:
        try:
            before_files = snapshot(os.path.join(WORKSPACE_BASEDIR, session_id))
        except Exception:
            logger.warning("[chat_service] 快照 session 工作目录失败", exc_info=True)

    # 执行编排主流程（携带 agent_id，若不为空则走单 agent 直接问答）
    async for event_str in orchestrator_service.run(
        full_messages,
        session_id=session_id,
        user_id=user_id,
        session_service=session_service,
        agent_id=agent_id,
        request=request,
    ):
        yield event_str

        # 解析事件，提取 summary 作为最终输出，并检测 CUSTOM_COMPONENT
        try:
            if event_str.startswith("data: ") and event_str.endswith("\n\n"):
                payload = json.loads(event_str[6:].strip())
                event_type = payload.get("type", "")

                # 汇总事件 → 收集输出
                if event_type == "summary":
                    final_output_parts.append(payload.get("content", ""))

                # 检测工具结果中的组件 → 转发 CUSTOM_COMPONENT
                if event_type == "TOOL_RESULT_TEXT_DELTA":
                    delta = payload.get("delta", "")
                    for component in _extract_components_from_delta(delta):
                        yield f"data: {json.dumps(component, ensure_ascii=False)}\n\n"
        except Exception:
            logger.debug("[chat_service] 事件解析跳过", exc_info=True)

    final_output = "\n".join(final_output_parts).strip()

    # 持久化对话历史（用户输入 + 智能体输出）
    if session_service and session_id and user_id:
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            user_input = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        user_input = "\n".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        user_input = str(content)
                    break

            new_messages = []
            if user_input:
                new_messages.append({
                    "role": "user", "content": user_input, "timestamp": now_str,
                })
            if final_output:
                new_messages.append({
                    "role": "assistant", "content": final_output, "timestamp": now_str,
                })
            if new_messages:
                await session_service.append_messages(
                    session_id, user_id, new_messages
                )
        except Exception:
            logger.exception("[chat_service] 持久化对话历史失败")

    # ---- 检测本轮新文件并 yield files_generated 事件 ----
    files_payload = []
    try:
        after_files = snapshot(os.path.join(WORKSPACE_BASEDIR, session_id)) if session_id else set()
        new_files = diff(before_files, after_files)
        for rel_path in new_files:
            meta = build_file_meta(
                os.path.join(WORKSPACE_BASEDIR, session_id), rel_path, session_id
            )
            if meta is not None:
                files_payload.append(meta)
    except Exception:
        logger.warning("[chat_service] 检测新文件失败", exc_info=True)
    yield f"data: {json.dumps({'type': 'files_generated', 'files': files_payload}, ensure_ascii=False)}\n\n"

    # 更新 Langfuse observation 并发送 TRACE_READY 事件
    trace_id = None
    if obs and langfuse_service:
        try:
            langfuse_service.end_observation(
                obs,
                output={
                    "reply": final_output,
                    "session_id": session_id,
                    "user_id": user_id,
                },
            )
            langfuse_service.flush()
            trace_id = obs.trace_id if obs else None
        except Exception:
            pass

    trace_event = json.dumps({"type": "trace_ready", "trace_id": trace_id})
    yield f"data: {trace_event}\n\n"

    # 保存 trace_id 到 Redis 元信息
    if session_service and session_id and trace_id:
        await session_service.save_latest_trace_id(session_id, trace_id)
