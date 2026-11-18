"""上传文件即时解析服务。

上传接口收到文件后立即返回，由本服务在后台完成解析并写入 upload_files 表：
- 图片/文档/pdf/表格 → MinerU（提交 + 轮询，逻辑迁移自原 tools/mineru_tools.py）
- 音频 → OpenAI 兼容 /v1/chat/completions（音频 base64 data URI 传入，默认 Qwen3-ASR-1.7B）
- 纯文本 → 直接解码
- 其余 → 只入库不解析（不注入提示词）

成功与失败都写 parsed_content：失败/超时写入提示文案，
问答注入时 agent 能诚实告知用户该文件解析失败。
"""
import asyncio
import base64
import logging
import uuid
from pathlib import Path

import httpx

from app.config import (
    MINERU_API_KEY,
    MINERU_BASE_URL,
    UPLOAD_PARSE_CONCURRENCY,
    UPLOAD_MAX_INFLIGHT,
    UPLOAD_PARSE_TIMEOUT,
)

logger = logging.getLogger(__name__)

# 后台解析并发信号量：模块级单例，超出上限的任务排队等待
_PARSE_SEMAPHORE = asyncio.Semaphore(UPLOAD_PARSE_CONCURRENCY)


class UploadAdmission:
    """事件循环内无 await 的接纳计数；读取正文前拒绝超量请求。"""

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("上传接纳容量必须大于 0")
        self.capacity = capacity
        self.active = 0

    def acquire(self):
        from fastapi import HTTPException
        if self.active >= self.capacity:
            raise HTTPException(status_code=429, detail="文件解析繁忙，请稍后重试", headers={"Retry-After": "5"})
        self.active += 1
        return _UploadLease(self)


class _UploadLease:
    def __init__(self, owner):
        self.owner = owner
        self.released = False

    def release(self):
        if not self.released:
            self.released = True
            self.owner.active -= 1


_UPLOAD_ADMISSION = UploadAdmission(UPLOAD_MAX_INFLIGHT)
_PARSE_TASKS: set[asyncio.Task] = set()


def acquire_upload_slot():
    return _UPLOAD_ADMISSION.acquire()

# 音频扩展名（media_type 可能缺失或不规范，扩展名兜底）
_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".webm", ".aac", ".ogg", ".flac", ".wma"}

# 扩展名 → data URI 的 mime（chat 接口按 base64 data URI 传音频）
_AUDIO_MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".wma": "audio/x-ms-wma",
}

# MinerU 支持的文档/图片扩展名（与原 tools/mineru_tools.SUPPORTED_EXTENSIONS 一致）
_MINERU_EXTENSIONS = {
    ".pdf", ".doc", ".docx",
    ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pptx",
}

_MINERU_POLL_INTERVAL = 2  # 秒
_MINERU_POLL_MAX_RETRIES = 2  # 单次结果查询断连后最多重试两次，不重新提交任务

# 统一超时/失败文案（诚实入库，问答时注入给 agent）
_MINERU_TIMEOUT_MSG = "解析超时，MinerU服务暂时无法解析该文件"
_ASR_TIMEOUT_MSG = "解析超时，音频解析服务暂时无法解析该文件"


def classify_parse_type(media_type: str, filename: str) -> str:
    """按 media_type（优先）与扩展名（兜底）判定解析方式。

    Returns:
        "mineru" | "asr" | "plain_text" | "none"
    """
    ext = Path(filename or "").suffix.lower()
    if (media_type or "").startswith("audio/") or ext in _AUDIO_EXTENSIONS:
        return "asr"
    if (media_type or "").startswith("image/"):
        return "mineru"
    if (media_type or "") in {"text/plain", "text/csv"} or ext in {".txt", ".md", ".csv"}:
        return "plain_text"
    if ext in _MINERU_EXTENSIONS:
        return "mineru"
    # 常见文档类型按 media_type 兜底（扩展名缺失时）
    doc_media_types = {
        "application/pdf",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    if (media_type or "") in doc_media_types:
        return "mineru"
    return "none"


async def parse_document_with_mineru(content: bytes, filename: str) -> str:
    """调 MinerU 解析文档 bytes，返回 Markdown；失败/超时返回提示文案。

    提交 POST {base}/tasks（multipart，鉴权头 x-api-key），
    轮询 GET {base}/tasks/{id}/result，总超时 UPLOAD_PARSE_TIMEOUT 秒。
    """
    if not MINERU_API_KEY or not MINERU_BASE_URL:
        return "解析失败，MinerU服务未配置（缺少 MINERU_API_KEY / MINERU_BASE_URL）"

    headers = {"x-api-key": MINERU_API_KEY}
    base = MINERU_BASE_URL.rstrip("/")

    try:
        # 1) 提交任务
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/tasks",
                headers=headers,
                files={"file": (filename, content)},
                data={"output_formats": "md"},
            )
            if resp.status_code != 201:
                logger.warning(
                    "[upload-parse] MinerU 提交失败 HTTP %s body=%s (%s)",
                    resp.status_code, resp.text[:500], filename,
                )
                try:
                    detail = (resp.json() or {}).get("detail", "")
                except Exception:
                    detail = ""
                suffix = f"：{detail}" if detail else ""
                return f"解析失败，MinerU服务暂时无法解析该文件（HTTP {resp.status_code}{suffix}）"
            task_id = resp.json().get("id")
            if not task_id:
                return "解析失败，MinerU服务暂时无法解析该文件（提交响应缺少 task id）"
            logger.info(f"[upload-parse] MinerU 任务已提交: {task_id} ({filename})")

        # 2) 轮询结果（总超时 UPLOAD_PARSE_TIMEOUT 秒）
        elapsed = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            while elapsed < UPLOAD_PARSE_TIMEOUT:
                for attempt in range(_MINERU_POLL_MAX_RETRIES + 1):
                    try:
                        r = await client.get(
                            f"{base}/tasks/{task_id}/result", headers=headers
                        )
                        break
                    except httpx.RemoteProtocolError:
                        if attempt >= _MINERU_POLL_MAX_RETRIES:
                            raise
                        logger.warning(
                            "[upload-parse] MinerU 查询结果连接中断，重试原任务 "
                            "task_id=%s retry=%s/%s",
                            task_id, attempt + 1, _MINERU_POLL_MAX_RETRIES,
                        )
                        await asyncio.sleep(_MINERU_POLL_INTERVAL)
                        elapsed += _MINERU_POLL_INTERVAL
                        if elapsed >= UPLOAD_PARSE_TIMEOUT:
                            return _MINERU_TIMEOUT_MSG
                if r.status_code != 200:
                    logger.warning(
                        "[upload-parse] MinerU 查询失败 HTTP %s body=%s task=%s",
                        r.status_code, r.text[:500], task_id,
                    )
                    return f"解析失败，MinerU服务暂时无法解析该文件（HTTP {r.status_code}）"
                data = r.json()
                status = data.get("status")
                if status == "completed":
                    md = (data.get("result") or {}).get("md_content")
                    if not md:
                        logger.warning(
                            "[upload-parse] MinerU 解析完成但内容为空 data=%s task=%s",
                            str(data)[:500], task_id,
                        )
                        return "解析失败，MinerU 解析完成但内容为空"
                    return md
                if status == "failed":
                    err = data.get("error_message", "未知错误")
                    return f"解析失败，MinerU任务失败：{err}"
                # pending / running → 继续等
                await asyncio.sleep(_MINERU_POLL_INTERVAL)
                elapsed += _MINERU_POLL_INTERVAL

        return _MINERU_TIMEOUT_MSG
    except Exception as e:
        logger.exception("[upload-parse] MinerU 解析异常")
        return f"解析失败，MinerU服务暂时无法解析该文件（{e}）"


async def transcribe_audio(content: bytes, filename: str, file_id: int | None = None) -> str:
    """调音频转写模型（OpenAI 兼容 /v1/chat/completions），返回转写文本。

    接口与其他文本模型一致（chat completions 形态）：音频以 base64
    data URI 放在 messages[0].content 的 audio_url 分片。模型配置来自
    model_config.yml 的 models.audio_transcriber（默认 Qwen3-ASR-1.7B）。
    请求头带全局流水号 app_serial_number = OIA-AGENTSCOPE-UPLOAD-{file_id}
    （与业务对话 LLM 的 OIA-AGENTSCOPE-{message_pair_id} 同一绑定方式）。
    失败/超时返回提示文案。
    """
    # 延迟导入避免模块级连锁依赖
    from app.services.chat_service import load_model_config

    try:
        cfg = (load_model_config().get("models") or {}).get("audio_transcriber") or {}
    except Exception:
        logger.exception("[upload-parse] 读取 audio_transcriber 配置失败")
        cfg = {}

    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model_name = cfg.get("model_name") or "Qwen3-ASR-1.7B"
    params = cfg.get("parameters") or {}

    if not base_url:
        return "解析失败，音频解析模型未配置（model_config.yml 缺少 audio_transcriber.base_url）"

    timeout = int(params.get("timeout") or UPLOAD_PARSE_TIMEOUT)
    # 接口与其他文本模型同一约定：base_url 含 /v1；漏写时自动补齐，
    # 避免拼出 .../v1/v1/... 双 /v1 导致 404
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    url = f"{base_url}/chat/completions"
    ext = Path(filename).suffix.lower()
    mime = _AUDIO_MIME_BY_EXT.get(ext, "audio/mpeg")
    audio_uri = "data:" + mime + ";base64," + base64.b64encode(content).decode("ascii")
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "audio_url", "audio_url": {"url": audio_uri}},
                ],
            }
        ],
    }
    try:
        # 全局流水号：与业务对话 LLM 调用同一绑定方式（请求头注入）；
        # 上传解析无 message_pair_id，用 file_id 关联 upload_files 行，
        # 缺省回退 uuid
        pair = file_id if file_id is not None else uuid.uuid4().hex
        headers = {
            "Authorization": f"Bearer {api_key}",
            "app_serial_number": f"OIA-AGENTSCOPE-UPLOAD-{pair}",
        }
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            resp = await client.post(
                url,
                headers=headers,
                json=payload,
            )
        if resp.status_code != 200:
            logger.warning(
                "[upload-parse] ASR HTTP %s url=%s body=%s",
                resp.status_code, url, resp.text[:500],
            )
            return f"解析失败，音频解析服务暂时无法解析该文件（HTTP {resp.status_code}）"
        choices = (resp.json() or {}).get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        text = message.get("content") or ""
        if not text:
            return "解析失败，音频解析服务返回空转写结果"
        return text
    except httpx.TimeoutException:
        return _ASR_TIMEOUT_MSG
    except Exception as e:
        logger.exception("[upload-parse] 音频转写异常")
        return f"解析失败，音频解析服务暂时无法解析该文件（{e}）"


async def _parse_and_store(dao, file_id: int, content: bytes, filename: str, parse_type: str) -> None:
    """后台解析并写库（含 Langfuse 埋点）。任何异常都收敛为入库的失败文案。"""
    from app.services.langfuse_service import derive_trace_id, get_current_langfuse

    langfuse_service = get_current_langfuse()
    # 后台任务运行在请求上下文之外，upload-parse 是裸根 span：
    # 传确定性 trace_id（从 file_id 派生）让 SDK 标 AS_ROOT，span 直接
    # 映射为 trace 记录本体，不依赖服务端对裸根 span 的推断（对齐
    # kb_search 模式，避免部分环境退化成 unnamed trace）
    span_ctx = (
        langfuse_service.start_span(
            "upload-parse",
            input={
                "filename": filename,
                "parse_type": parse_type,
                "size": len(content),
            },
            trace_id=derive_trace_id(f"upload-{file_id}"),
        )
        if langfuse_service and getattr(langfuse_service, "enabled", False)
        else None
    )
    span = None
    try:
        if span_ctx is not None:
            span = span_ctx.__enter__()

        await dao.mark_parsing(file_id)

        if parse_type == "mineru":
            parsed = await parse_document_with_mineru(content, filename)
        elif parse_type == "asr":
            parsed = await transcribe_audio(content, filename, file_id)
        elif parse_type == "plain_text":
            try:
                parsed = content.decode("utf-8")
            except UnicodeDecodeError:
                parsed = content.decode("gbk", errors="replace")
        else:
            parsed = None

        if parsed is None:
            # none 类型：不解析、不注入
            await dao.update_parse_result(file_id, "completed", None)
        elif parsed.startswith(("解析失败", "解析超时")):
            # 诚实入库：失败/超时文案同样写入 parsed_content，问答时 agent 可告知用户
            await dao.update_parse_result(file_id, "failed", parsed, error_message=parsed[:1000])
        else:
            await dao.update_parse_result(file_id, "completed", parsed)

        if span:
            try:
                preview = (parsed or "")[:200]
                span.update(output={
                    "status": "completed" if (parsed and not parsed.startswith(("解析失败", "解析超时"))) else "failed",
                    "content_len": len(parsed or ""),
                    "preview": preview,
                })
            except Exception:
                pass
        logger.info(
            "[upload-parse] 完成 file_id=%s filename=%s type=%s content_len=%s",
            file_id, filename, parse_type, len(parsed or ""),
        )
    except Exception:
        logger.exception("[upload-parse] 后台解析异常 file_id=%s", file_id)
        try:
            await dao.update_parse_result(
                file_id, "failed", "解析失败，服务内部异常", error_message="internal error"
            )
        except Exception:
            logger.exception("[upload-parse] 失败状态回写也失败 file_id=%s", file_id)
    finally:
        if span_ctx is not None:
            try:
                span_ctx.__exit__(None, None, None)
            except Exception:
                pass


async def _parse_and_store_limited(
    dao, file_id: int, content: bytes, filename: str, parse_type: str
) -> None:
    """信号量限流后执行解析：超出并发上限的任务在后台排队，不影响上传响应。"""
    async with _PARSE_SEMAPHORE:
        await _parse_and_store(dao, file_id, content, filename, parse_type)


async def start_background_parse(
    request, session_id: str, user_id: str, filename: str, media_type: str, content: bytes,
    *, admission=None,
) -> int:
    """插入 pending 记录并启动后台解析任务，立即返回 file_id。

    Args:
        request: FastAPI Request（取 app.state.upload_file_dao）
        user_id: 上传者（写入 upload_files.user_id，供按用户查询）
    """
    dao = getattr(request.app.state, "upload_file_dao", None)
    if dao is None:
        raise RuntimeError("upload_file_dao 未初始化（app.state.upload_file_dao 缺失）")

    admission = admission or acquire_upload_slot()
    try:
        parse_type = classify_parse_type(media_type, filename)
        file_id = await dao.insert(
            session_id, user_id, filename, media_type, parse_type, len(content)
        )
        task = asyncio.create_task(
            _parse_and_store_limited(dao, file_id, content, filename, parse_type),
            name=f"upload-parse-{file_id}",
        )
        _PARSE_TASKS.add(task)
        # done 回调也覆盖任务尚未开始执行就被取消的情况。
        def finished(completed):
            _PARSE_TASKS.discard(completed)
            admission.release()
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(finished)
        return file_id
    except BaseException:
        admission.release()
        raise
