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

from app.config import UPLOAD_PARSE_TIMEOUT

logger = logging.getLogger(__name__)

# 上传文件解析内容注入提示词的头部标记与单文件截断上限（防超上下文）
_UPLOAD_CTX_HEADER = "【用户本轮上传文件解析内容】"
_UPLOAD_CTX_MAX_CHARS = 30000

# 提问时若上传文件仍在解析：轮询等待的总超时与间隔（秒）。
# 等待窗口与解析侧统一超时（UPLOAD_PARSE_TIMEOUT）保持一致——若等待窗口
# 早于解析真正结束，会把"仍在解析"误报为"解析超时"（图片走 MinerU OCR
# 常超过 15s）。超时后不再等待，改为在提示词中注入未完成提示。
_UPLOAD_WAIT_TIMEOUT = float(UPLOAD_PARSE_TIMEOUT)
_UPLOAD_WAIT_POLL_INTERVAL = 1.0

# 等待窗口结束后 status 仍为 pending/parsing 的文件：注入"仍在解析中"提示，
# 而非"解析超时/解析失败"。二者的区别很关键——真正失败的文件，其解析服务会在
# 统一超时（UPLOAD_PARSE_TIMEOUT）内把失败文案写进 parsed_content，走 rows 分支；
# 仍出现在这里的是排队未开始或后台任务尚未收尾的文件，尚未失败。
# 若此处误报"解析超时"，模型会把"其实解析成功、只是慢"的文件说成失败。
_UPLOAD_STILL_PARSING_HINTS = {
    "mineru": "该文件仍在解析中（MinerU 解析尚未完成），请告知用户稍后重试，不要声称该文件解析失败或超时。",
    "asr": "该文件仍在解析中（音频转写尚未完成），请告知用户稍后重试，不要声称该文件解析失败或超时。",
}
_UPLOAD_STILL_PARSING_HINT_DEFAULT = (
    "该文件仍在解析中，请告知用户稍后重试，不要声称该文件解析失败或超时。"
)

# 解析失败文案约定：真正解析失败的文件，其正文即以「解析失败」/「解析超时」开头。
_UPLOAD_FAILURE_NOTICE = (
    "说明：若某文件正文以「解析失败」或「解析超时」开头，表示该文件确实未能解析"
    "成功，请如实告知用户，不要编造其内容；其余文件为正常解析结果。"
)

# 文件状态 → 展示标记（档案/提示共用）
_STATUS_MARKS = {
    "completed": "解析成功",
    "failed": "解析失败",
    "parsing": "解析中",
    "pending": "解析中",
}


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


async def load_upload_context(
    request: Any, session_id: Optional[str], sandbox_files: Optional[list[str]] = None
) -> str:
    """检索该会话未绑定消息的上传文件解析内容，拼接为提示词片段。

    sandbox_files：已成功写入沙箱的解析文件相对路径（persist_upload_files_to_workspace
    的返回值）。非空时注入"已保存到沙箱可读取"的就近声明，空时注入"不在沙箱勿查找"。

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
    # 等待窗口结束后仍未完成解析的文件：注入"仍在解析中"提示（agent 如实告知用户稍后重试）
    try:
        parsing_rows = await dao.load_unbound_parsing(session_id)
    except Exception:
        logger.warning("[OrchestratorService] 检索解析中上传文件失败", exc_info=True)
        parsing_rows = []
    if not rows and not parsing_rows:
        # 本轮无新文件：仍返回会话历史文件档案（若有），供历史文件指代寻址
        return await _build_session_archive(dao, session_id)
    parts = [_UPLOAD_CTX_HEADER]
    # 历史文件档案前置并降级：此前档案拼在整个块末尾，是离模型生成最近的强
    # 信号，会压过"以本轮文件为主"的声明，使模型优先去读沙箱里的历史文件
    # （多轮上传串答的根因之一）。前置 + 降级为"仅点名才用"后，
    # 离模型最近的强信号变成本轮文件声明与末尾锚点。
    archive = await _build_session_archive(dao, session_id, has_new_files=True)
    if archive:
        parts.append(archive)
    # 就近约束：写在任务现场（用户问题尾部）的指令约束力远高于系统提示词/技能里
    # 的远处声明——直接决定模型是否会去沙箱文件系统找文件。
    # 同时做指代消歧：沙箱 uploads/ 与对话历史里可能残留此前轮次的旧文件，
    # 必须明确"这个文件"默认指本轮新上传的文件（多轮上传串答的根因之一）
    if sandbox_files:
        parts.append(
            "以下是用户本轮新上传、此前未处理过的文件（解析文本已同步保存到"
            "沙箱工作目录 uploads/ 下，文件名为「原文件名.md」（保留原扩展名，"
            "如 报告.pdf → uploads/报告.pdf.md）；上传的原件（图片/PDF 等本体）"
            "不会出现在沙箱，沙箱里只有该 .md 文本，可直接读取处理；"
            "请基于以下文本或沙箱中对应 .md 文件回答，不要向用户表示找不到文件）。"
            "沙箱 uploads/ 目录中还可能留有此前轮次的历史文件，对话历史里也可能"
            "出现过旧文件内容；除非用户明确点名，回答应基于本轮新上传的文件——"
            "用户提问指代不明确（如\"这个文件\"）时，默认指本轮新上传的文件。"
        )
    else:
        parts.append(
            "注意：以下为用户本轮新上传、此前未处理过的文件。这些文件的原件"
            "（图片/PDF 等本体）与解析文本均不在沙箱文件系统中（沙箱 uploads/ "
            "里可能只有此前轮次的旧文件副本，见上方档案），请直接基于以下文本"
            "回答，不要尝试在沙箱中查找本轮新上传的文件，也不要向用户表示"
            "找不到文件。用户提问指代不明确（如\"这个文件\"）时，默认指本轮新"
            "上传的文件，不要回答成对话历史中出现过的旧文件内容。"
        )
    # 失败文案约定：无论文件是否落盘沙箱，都提示模型正确解读失败文案
    parts.append(_UPLOAD_FAILURE_NOTICE)
    for row in rows:
        content = (row.get("parsed_content") or "")[:_UPLOAD_CTX_MAX_CHARS]
        parts.append(f"=== 文件名: {row.get('filename', '')} ===")
        parts.append(content)
    for row in parsing_rows:
        hint = _UPLOAD_STILL_PARSING_HINTS.get(
            row.get("parse_type"), _UPLOAD_STILL_PARSING_HINT_DEFAULT
        )
        parts.append(f"=== 文件名: {row.get('filename', '')} ===")
        parts.append(hint)
    # 末尾锚点：让"围绕本轮文件回答"成为离模型生成最近的指令，
    # 避免模型被档案/历史里的旧文件内容带偏（多轮上传串答）
    parts.append("本轮请围绕上述本轮上传的文件回答。")
    return "\n".join(parts)


def append_upload_context(text: str, upload_ctx: str) -> str:
    """把上传文件上下文追加到文本尾部（上下文为空时原样返回）。"""
    if not upload_ctx:
        return text
    return f"{text}\n\n{upload_ctx}"


def _sandbox_safe_name(filename: str) -> str:
    """文件名安全化：去路径部分，非法字符替换为下划线，保留中文字符。"""
    import re

    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    safe = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", base).strip("._")
    return safe or "upload.md"


async def persist_upload_files_to_workspace(
    request: Any,
    session_id: Optional[str],
    user_id: Optional[str],
    workspace_manager: Any,
) -> dict:
    """把未消费上传文件的解析文本写入沙箱工作目录（uploads/xxx.md）。

    模型习惯到沙箱文件系统找用户上传的文件（原件不落沙箱、只注入
    截断文本，导致"找不到文件"的用户可见问题）；本函数把解析文本
    落成沙箱实体文件，让模型"找了也找得到"。

    返回 {"file_ids": 本轮消费的文件 id 列表, "sandbox_files": 成功
    写入的相对路径列表}：file_ids 供轮末 bind_message_id 精确绑定
    （只绑本轮真正消费过的文件）。任何失败仅告警并跳过，降级为
    纯注入模式（load_upload_context 走 sandbox_files=None 分支）。

    另含沙箱自愈：沙箱被淘汰重建后会话目录为空，已绑定文件不会再
    走注入链路；对沙箱中缺失的历史解析文件做幂等补写。
    """
    empty: dict = {"file_ids": [], "sandbox_files": []}
    if not (request and session_id and user_id and workspace_manager):
        return empty
    dao = _get_upload_dao(request)
    if dao is None:
        return empty
    try:
        rows = await dao.load_unbound_parsed(session_id)
    except Exception:
        logger.warning(
            "[upload_persist] 查询未消费上传文件失败，跳过沙箱写入", exc_info=True
        )
        return empty
    file_ids: list = []
    written: list[str] = []
    for row in rows:
        content = row.get("parsed_content") or ""
        if not content:
            continue
        fid = row.get("id")
        if fid is not None:
            file_ids.append(fid)
        rel = f"uploads/{_sandbox_safe_name(row.get('filename', ''))}.md"
        try:
            ok = await workspace_manager.write_session_file(
                user_id, session_id, rel, content
            )
        except Exception:
            logger.warning(
                f"[upload_persist] 写入沙箱失败 rel={rel}，跳过", exc_info=True
            )
            continue
        if ok:
            written.append(rel)
    if written:
        logger.info(
            "[upload_persist] 已写入 %s 个解析文件到沙箱 uploads/: %s",
            len(written), written,
        )
    await _heal_missing_upload_files(dao, workspace_manager, user_id, session_id)
    return {"file_ids": file_ids, "sandbox_files": written}


async def _heal_missing_upload_files(
    dao: Any,
    workspace_manager: Any,
    user_id: str,
    session_id: str,
) -> None:
    """沙箱自愈：补写已绑定但沙箱中缺失的历史解析文件。

    沙箱重建后 /data/workspaces/{session} 为空目录，而正常注入链路只
    处理未绑定文件——历史文件将永久"消失"。此处按 list_session_files
    的现状做缺失补写（幂等：已存在则跳过），失败仅告警不影响主流程。
    """
    try:
        all_rows = await dao.load_all_parsed(session_id)
    except Exception:
        logger.warning(
            "[upload_persist] 查询会话历史解析文件失败，跳过自愈", exc_info=True
        )
        return
    if not all_rows:
        return
    try:
        existing = await workspace_manager.list_session_files(user_id, session_id)
    except Exception:
        logger.warning(
            "[upload_persist] 列举沙箱会话文件失败，跳过自愈", exc_info=True
        )
        return
    healed: list[str] = []
    for row in all_rows:
        if row.get("message_id") is None:
            continue  # 未绑定文件本轮已正常写入，无需自愈
        rel = f"uploads/{_sandbox_safe_name(row.get('filename', ''))}.md"
        if rel in existing:
            continue
        try:
            ok = await workspace_manager.write_session_file(
                user_id, session_id, rel, row.get("parsed_content") or ""
            )
        except Exception:
            continue
        if ok:
            healed.append(rel)
    if healed:
        logger.info(
            "[upload_persist] 沙箱自愈补写 %s 个历史解析文件: %s",
            len(healed), healed,
        )


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


async def _build_session_archive(
    dao: Any, session_id: str, *, has_new_files: bool = False
) -> str:
    """构建"本会话历史已处理文件"档案片段（仅已绑定 message_id 的文件）。

    只列文件名与沙箱副本路径、不注入全文，避免上下文膨胀。作用：
    后续轮次（含跨 agent 路由）点名/指代历史文件时，agent 可按档案
    直接读取沙箱 uploads/ 副本，弥补"注入只进当轮 agent 私有记忆"
    的跨轮断链。检索失败静默返回空串。

    has_new_files：本轮是否已注入新上传文件。为 True 时档案降级为
    "仅用户点名才用"，避免历史文件压过本轮文件、使模型去找旧材料
    回答本轮问题（多轮上传串答）。
    """
    try:
        rows = await dao.load_session_file_index(session_id)
    except Exception:
        logger.warning("[OrchestratorService] 检索会话文件档案失败", exc_info=True)
        return ""
    bound = [r for r in rows if r.get("message_id") is not None]
    if not bound:
        return ""
    lines = [
        "【本会话历史已处理文件】",
        "以下文件在此前轮次已处理过，解析副本保存在沙箱工作目录 uploads/ 下：",
    ]
    for r in bound:
        fn = r.get("filename", "")
        mark = _STATUS_MARKS.get(r.get("status"), r.get("status") or "未知状态")
        lines.append(f"- {fn}（{mark}，沙箱副本: uploads/{_sandbox_safe_name(fn)}.md）")
    if has_new_files:
        # 本轮已注入新文件：档案降级为"仅点名才用"，避免历史文件
        # 压过本轮文件、使模型去找旧材料回答本轮问题
        lines.append(
            "本轮已在上方提供新上传的文件，请以本轮文件为准回答；除非用户"
            "明确点名上述历史文件（如\"刚才那个文件\"），否则不要读取历史"
            "文件副本，也不要基于历史文件内容回答本轮问题。"
        )
    else:
        lines.append(
            "用户点名或指代上述历史文件（如\"刚才那个文件\"）时，请先读取对应"
            "沙箱副本获取内容再处理；本轮未点名历史文件时无需读取。"
        )
    return "\n".join(lines)


async def build_recognizer_file_hint(request: Any, session_id: Optional[str]) -> str:
    """为意图识别构建"本轮上传文件"结构化提示（防历史文件名脑补）。

    意图识别 LLM 看得到历史上下文，曾把历史回复中的旧文件名补进子
    query（如"润色下<上轮失败文件>"）导致串答。此提示给出本轮未消费
    文件清单 + 约束：文件指代只能用本轮清单中的文件名；本轮为空时
    query 不得包含任何文件名。检索失败静默返回空串（不影响原行为）。
    """
    if request is None or not session_id:
        return ""
    dao = _get_upload_dao(request)
    if dao is None:
        return ""
    try:
        rows = await dao.load_session_file_index(session_id)
    except Exception:
        logger.warning("[OrchestratorService] 检索意图识别文件提示失败", exc_info=True)
        return ""
    if not rows:
        return ""
    current = [r for r in rows if r.get("message_id") is None]
    lines = ["【本轮新上传文件（未消费）】"]
    if current:
        for r in current:
            mark = _STATUS_MARKS.get(r.get("status"), r.get("status") or "未知状态")
            lines.append(f"- {r.get('filename', '')}（{mark}）")
    else:
        lines.append("无")
    lines.append(
        "约束：生成子意图 query 时，文件指代只能引用上面清单中的文件名；"
        "清单为\"无\"时，query 不得包含任何文件名，也不要从历史上下文中"
        "引用旧文件名。"
    )
    return "\n".join(lines)
