"""上传文件上下文模块：解析内容注入提示词 + 解析等待轮询。

从 orchestrator_service.py 外移的纯函数，只读
request.app.state.upload_file_dao，不依赖服务实例状态。
"""
import asyncio
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Optional

from agentscope.event import ToolCallStartEvent, ToolCallEndEvent

logger = logging.getLogger(__name__)

# 上传文件解析内容注入提示词的头部标记与单文件截断上限（防超上下文）
_UPLOAD_CTX_HEADER = "【用户上传文件解析内容】"
_UPLOAD_CTX_MAX_CHARS = 30000

# 提问时若上传文件仍在解析：轮询等待的总超时与间隔（秒）。
# 超时后不再等待，改为在提示词中注入解析失败提示。
_UPLOAD_WAIT_TIMEOUT = 15.0
_UPLOAD_WAIT_POLL_INTERVAL = 1.0

# 等待超时后仍在解析中的文件，按 parse_type 注入的失败提示文案
_UPLOAD_PARSE_TIMEOUT_HINTS = {
    "mineru": "解析超时，MinerU服务暂时无法解析该文件",
    "asr": "解析超时，音频解析服务暂时无法解析该文件",
}
_UPLOAD_PARSE_TIMEOUT_HINT_DEFAULT = "解析超时，暂时无法解析该文件"


def _get_upload_dao(request: Any):
    """从 app.state 取 upload_file_dao，无 request 时返回 None。"""
    if request is None:
        return None
    return getattr(request.app.state, "upload_file_dao", None)


async def wait_for_upload_parsing(
    request: Any, session_id: Optional[str]
) -> AsyncGenerator[str, None]:
    """存在解析中的未绑定上传文件时，轮询等待其完成（最多 _UPLOAD_WAIT_TIMEOUT 秒）。

    等待期间发一对 TOOL_CALL_START/TOOL_CALL_END 事件（tool_call_name
    "等待mineru文件解析完成"，id 随机造，仅用于前端展示）；
    DAO 异常静默结束（不等待、不发事件），不影响问答主流程。
    """
    if request is None or not session_id:
        return
    dao = _get_upload_dao(request)
    if dao is None:
        return
    try:
        parsing = await dao.load_unbound_parsing(session_id)
    except Exception:
        logger.warning("[OrchestratorService] 查询解析中上传文件失败", exc_info=True)
        return
    if not parsing:
        return

    reply_id = f"upload-wait-{uuid.uuid4().hex[:12]}"
    tool_call_id = f"upload-wait-{uuid.uuid4().hex[:12]}"
    yield (
        "data: "
        + ToolCallStartEvent(
            reply_id=reply_id,
            tool_call_id=tool_call_id,
            tool_call_name="等待mineru文件解析完成",
            metadata={"files": [r.get("filename", "") for r in parsing]},
        ).model_dump_json()
        + "\n\n"
    )
    deadline = time.monotonic() + _UPLOAD_WAIT_TIMEOUT
    while True:
        await asyncio.sleep(_UPLOAD_WAIT_POLL_INTERVAL)
        try:
            parsing = await dao.load_unbound_parsing(session_id)
        except Exception:
            logger.warning("[OrchestratorService] 轮询解析状态失败，停止等待", exc_info=True)
            break
        if not parsing:
            break
        if time.monotonic() >= deadline:
            break
    yield (
        "data: "
        + ToolCallEndEvent(
            reply_id=reply_id,
            tool_call_id=tool_call_id,
        ).model_dump_json()
        + "\n\n"
    )


async def load_upload_context(request: Any, session_id: Optional[str]) -> str:
    """检索该会话未绑定消息的上传文件解析内容，拼接为提示词片段。

    上传文件在 /upload 时即后台解析入库（见 file_parse_service）；
    此处只取 message_id IS NULL 且解析内容非空的记录（失败文案也算，
    让 agent 诚实告知用户）。检索失败静默返回空串，不影响问答主流程。
    """
    if request is None or not session_id:
        return ""
    dao = _get_upload_dao(request)
    if dao is None:
        return ""
    try:
        rows = await dao.load_unbound_parsed(session_id)
    except Exception:
        logger.warning("[OrchestratorService] 检索上传文件解析内容失败", exc_info=True)
        return ""
    # 等待超时后仍在解析中的文件：注入解析失败提示（agent 诚实告知用户）
    try:
        parsing_rows = await dao.load_unbound_parsing(session_id)
    except Exception:
        logger.warning("[OrchestratorService] 检索解析中上传文件失败", exc_info=True)
        parsing_rows = []
    if not rows and not parsing_rows:
        return ""
    parts = [_UPLOAD_CTX_HEADER]
    for row in rows:
        content = (row.get("parsed_content") or "")[:_UPLOAD_CTX_MAX_CHARS]
        parts.append(f"=== 文件名: {row.get('filename', '')} ===")
        parts.append(content)
    for row in parsing_rows:
        hint = _UPLOAD_PARSE_TIMEOUT_HINTS.get(
            row.get("parse_type"), _UPLOAD_PARSE_TIMEOUT_HINT_DEFAULT
        )
        parts.append(f"=== 文件名: {row.get('filename', '')} ===")
        parts.append(hint)
    return "\n".join(parts)


def append_upload_context(text: str, upload_ctx: str) -> str:
    """把上传文件上下文追加到文本尾部（上下文为空时原样返回）。"""
    if not upload_ctx:
        return text
    return f"{text}\n\n{upload_ctx}"


async def has_unbound_uploads(request: Any, session_id: Optional[str]) -> bool:
    """该会话是否存在未绑定消息的上传文件（用于跳过问题改写）。

    检索失败静默返回 False（不影响问答主流程，仅照常做改写）。
    """
    if request is None or not session_id:
        return False
    dao = _get_upload_dao(request)
    if dao is None:
        return False
    try:
        return await dao.has_unbound_files(session_id)
    except Exception:
        logger.warning("[OrchestratorService] 检查未绑定上传文件失败", exc_info=True)
        return False
