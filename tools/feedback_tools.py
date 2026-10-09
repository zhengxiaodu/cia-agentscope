"""卡片反馈工具：发卡 → 挂起等待用户反馈 → 返回反馈供 agent 同轮继续。

对应 spec: 卡片反馈暂停机制设计 4.2①。
产出格式与 render_generic_card 同构（type=chart + chartType），chat_service
的 _COMPONENT_TYPES 白名单无需改动即可被拦截转发为 CUSTOM_COMPONENT。
"""
import json
import logging
import uuid
from typing import Any, AsyncGenerator, Dict, Optional

from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, ToolChunk

from app.services.feedback_service import wait_feedback
from tools.tool_constants import RENDER_FEEDBACK_CARD

logger = logging.getLogger(__name__)


def _chunk(text: str, is_last: bool = False) -> ToolChunk:
    return ToolChunk(content=[TextBlock(type="text", text=text)], is_last=is_last)


async def _feedback_card_stream(
    session_id: str,
    card_type: str,
    schema: Dict[str, Any],
    title: str = "",
    timeout: Optional[float] = None,
) -> AsyncGenerator[ToolChunk, None]:
    """核心生成器：① yield 卡片 JSON（被 chat_service 拦截为 CUSTOM_COMPONENT）
    ② 挂起等待 ③ yield 用户反馈 JSON（is_last）。

    feedbackId 塞进 schema 下发，前端回传时据此对账。
    """
    feedback_id = str(uuid.uuid4())
    payload: Dict[str, Any] = {
        "type": "feedback_chart",
        "chartType": card_type,
        "schema": {**(schema or {}), "feedbackId": feedback_id},
    }
    if title:
        payload["title"] = title
    logger.info("[feedback_tools] 发卡并挂起 session=%s card_type=%s feedback_id=%s",
                session_id, card_type, feedback_id)
    yield _chunk(json.dumps(payload, ensure_ascii=False))

    result = await wait_feedback(session_id, feedback_id, timeout=timeout)

    logger.info("[feedback_tools] 收到反馈 session=%s feedback_id=%s status=%s",
                session_id, feedback_id, result.get("status", "ok"))
    yield _chunk(json.dumps(result, ensure_ascii=False), is_last=True)


def create_feedback_tool(session_id: str) -> FunctionTool:
    """创建卡片反馈 FunctionTool（闭包注入 session_id，同 policy_qa_tools 工厂模式）。

    Args:
        session_id: 当前会话 ID（与 POST /chat/feedback 回传的 session_id 对齐）
    """
    async def render_feedback_card(
        card_type: str,
        schema: Dict[str, Any],
        title: str = "",
    ) -> AsyncGenerator[ToolChunk, None]:
        """渲染交互卡片（确认/表单）并暂停等待用户反馈，收到反馈后返回给模型继续推理。

        卡片发出后本工具会挂起直到用户在卡片上提交反馈（confirm/cancel/submit），
        超时返回 {"status":"timeout"}。

        Args:
            card_type: 组件类型标识（管理端 componentType，如 feedback_confirm / feedback_form）
            schema: 卡片业务数据（字段结构见对应组件的 schemaFields）
            title: 卡片标题
        """
        async for chunk in _feedback_card_stream(session_id, card_type, schema, title):
            yield chunk

    return FunctionTool(render_feedback_card, name=RENDER_FEEDBACK_CARD)
