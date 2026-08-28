"""上传文件即时解析服务。

上传接口收到文件后立即返回，由本服务在后台完成解析并写入 upload_files 表：
- 图片/文档/pdf/表格 → MinerU（提交 + 轮询，逻辑迁移自原 tools/mineru_tools.py）
- 音频 → OpenAI 兼容 /v1/audio/transcriptions（默认 Qwen3-ASR-1.7B）
- 纯文本 → 直接解码
- 其余 → 只入库不解析（不注入提示词）

成功与失败都写 parsed_content：失败/超时写入提示文案，
问答注入时 agent 能诚实告知用户该文件解析失败。
"""
import asyncio
import logging
from pathlib import Path

import httpx

from app.config import MINERU_API_KEY, MINERU_BASE_URL, UPLOAD_PARSE_TIMEOUT

logger = logging.getLogger(__name__)

# 音频扩展名（media_type 可能缺失或不规范，扩展名兜底）
_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".webm", ".aac", ".ogg", ".flac", ".wma"}

# MinerU 支持的文档/图片扩展名（与原 tools/mineru_tools.SUPPORTED_EXTENSIONS 一致）
_MINERU_EXTENSIONS = {
    ".pdf", ".doc", ".docx",
    ".xls", ".xlsx", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
}

_MINERU_POLL_INTERVAL = 2  # 秒

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
    if (media_type or "") == "text/plain" or ext in {".txt", ".md"}:
        return "plain_text"
    if ext in _MINERU_EXTENSIONS:
        return "mineru"
    # 常见文档类型按 media_type 兜底（扩展名缺失时）
    doc_media_types = {
        "application/pdf",
        "application/msword",
        "text/csv",
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
                return f"解析失败，MinerU服务暂时无法解析该文件（HTTP {resp.status_code}）"
            task_id = resp.json().get("id")
            if not task_id:
                return "解析失败，MinerU服务暂时无法解析该文件（提交响应缺少 task id）"
            logger.info(f"[upload-parse] MinerU 任务已提交: {task_id} ({filename})")

        # 2) 轮询结果（总超时 UPLOAD_PARSE_TIMEOUT 秒）
        elapsed = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            while elapsed < UPLOAD_PARSE_TIMEOUT:
                r = await client.get(
                    f"{base}/tasks/{task_id}/result", headers=headers
                )
                if r.status_code != 200:
                    return f"解析失败，MinerU服务暂时无法解析该文件（HTTP {r.status_code}）"
                data = r.json()
                status = data.get("status")
                if status == "completed":
                    md = (data.get("result") or {}).get("md_content")
                    if not md:
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


async def transcribe_audio(content: bytes, filename: str) -> str:
    """调音频转写模型（OpenAI 兼容 /v1/audio/transcriptions），返回转写文本。

    模型配置来自 model_config.yml 的 models.audio_transcriber
    （默认 Qwen3-ASR-1.7B）。失败/超时返回提示文案。
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
    try:
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            resp = await client.post(
                f"{base_url}/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data={
                    "model": model_name,
                    "prompt": params.get("prompt", "解析音频"),
                    "language": params.get("language", "zh"),
                },
                files={"file": (filename, content)},
            )
        if resp.status_code != 200:
            return f"解析失败，音频解析服务暂时无法解析该文件（HTTP {resp.status_code}）"
        text = (resp.json() or {}).get("text") or ""
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
    from app.services.langfuse_service import get_current_langfuse

    langfuse_service = get_current_langfuse()
    span_ctx = (
        langfuse_service.start_span(
            "upload-parse",
            input={
                "filename": filename,
                "parse_type": parse_type,
                "size": len(content),
            },
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
            parsed = await transcribe_audio(content, filename)
        elif parse_type == "plain_text":
            parsed = content.decode("utf-8", errors="replace")
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


async def start_background_parse(
    request, session_id: str, filename: str, media_type: str, content: bytes
) -> int:
    """插入 pending 记录并启动后台解析任务，立即返回 file_id。

    Args:
        request: FastAPI Request（取 app.state.upload_file_dao）
    """
    dao = getattr(request.app.state, "upload_file_dao", None)
    if dao is None:
        raise RuntimeError("upload_file_dao 未初始化（app.state.upload_file_dao 缺失）")

    parse_type = classify_parse_type(media_type, filename)
    file_id = await dao.insert(session_id, filename, media_type, parse_type)

    # plain_text / none 无需异步网络调用，但仍走统一后台任务保证响应延迟一致
    asyncio.create_task(
        _parse_and_store(dao, file_id, content, filename, parse_type),
        name=f"upload-parse-{file_id}",
    )
    return file_id
