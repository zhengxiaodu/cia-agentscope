"""
MinerU 文档解析工具

调用 MinerU API Gateway 异步解析用户上传的文档（pdf / docx / doc / 表格 / 图片），
返回解析后的 Markdown 文本。

鉴权方式：请求头 `x-api-key`（非 Authorization）。
配置来源：`.env` 中的 MINERU_API_KEY / MINERU_BASE_URL，经 app.config 暴露。
"""
import asyncio
import logging
import os
from pathlib import Path

import httpx
from agentscope.tool import FunctionTool, ToolChunk

from app.config import MINERU_API_KEY, MINERU_BASE_URL

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_FORMATS = "md"
DEFAULT_TIMEOUT = 300        # 秒，沿用 MinerU 手册示例
DEFAULT_POLL_INTERVAL = 2    # 秒

# MinerU 支持的文件扩展名（仅用于入参提示与日志，实际校验由 MinerU magic bytes 完成）
SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx",
    ".xls", ".xlsx", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
}


def _normalize_file_path(file_path: str) -> str:
    """兼容 file:// URI 与裸本地路径，返回本地路径。"""
    if not file_path:
        raise ValueError("file_path 不能为空")
    if file_path.startswith("file://"):
        file_path = file_path[len("file://"):]
    return file_path


async def mineru_parse(
    file_path: str,
    output_formats: str = DEFAULT_OUTPUT_FORMATS,
    timeout: int = DEFAULT_TIMEOUT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
) -> ToolChunk:
    """调用 MinerU 解析本地文档（docx/doc/表格/图片/pdf），返回 Markdown 内容。

    Args:
        file_path: 本地文件路径或 file:// URI（来自上传组件）
        output_formats: 输出格式，逗号分隔
            （md / content_list / content_list_v2 / middle_json / model_output / images），默认 md
        timeout: 轮询最大等待秒数，默认 300
        poll_interval: 轮询间隔秒数，默认 2
    """
    # 1. 配置校验
    if not MINERU_API_KEY:
        return ToolChunk(text="错误：未配置 MINERU_API_KEY，请在 .env 中设置。")
    if not MINERU_BASE_URL:
        return ToolChunk(text="错误：未配置 MINERU_BASE_URL，请在 .env 中设置。")

    # 关键：x-api-key，非 Authorization
    headers = {"x-api-key": MINERU_API_KEY}
    base = MINERU_BASE_URL.rstrip("/")

    # 2. 路径归一化与存在性校验
    try:
        local_path = _normalize_file_path(file_path)
    except ValueError as e:
        return ToolChunk(text=f"错误：{e}")
    if not os.path.isfile(local_path):
        return ToolChunk(text=f"错误：文件不存在：{local_path}")

    ext = Path(local_path).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        logger.warning(f"[mineru] 扩展名 {ext} 不在推荐列表，仍尝试提交（由 MinerU 校验）")

    try:
        # 3. 提交任务（multipart/form-data）
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(local_path, "rb") as f:
                resp = await client.post(
                    f"{base}/tasks",
                    headers=headers,
                    files={"file": (os.path.basename(local_path), f)},
                    data={"output_formats": output_formats},
                )
            if resp.status_code != 201:
                return ToolChunk(
                    text=f"MinerU 提交失败: HTTP {resp.status_code} - {resp.text}"
                )
            task_id = resp.json().get("id")
            if not task_id:
                return ToolChunk(text=f"MinerU 提交响应缺少 task id: {resp.text}")
            logger.info(
                f"[mineru] 任务已提交: {task_id} ({os.path.basename(local_path)})"
            )

        # 4. 轮询结果
        elapsed = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            while elapsed < timeout:
                r = await client.get(
                    f"{base}/tasks/{task_id}/result",
                    headers=headers,
                )
                if r.status_code != 200:
                    return ToolChunk(
                        text=f"MinerU 轮询失败: HTTP {r.status_code} - {r.text}"
                    )
                data = r.json()
                status = data.get("status")
                if status == "completed":
                    result = data.get("result") or {}
                    md = result.get("md_content")
                    if not md:
                        # 兜底：result 为空时回传原始结构摘要
                        return ToolChunk(
                            text=f"MinerU 解析完成但 md_content 为空，原始结果: {data}"
                        )
                    return ToolChunk(text=md)
                if status == "failed":
                    err = data.get("error_message", "未知错误")
                    return ToolChunk(text=f"MinerU 任务失败: {err}")
                # pending / running → 继续等
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

        return ToolChunk(
            text=f"MinerU 任务超时：{task_id} 在 {timeout}s 内未完成。"
        )

    except httpx.HTTPError as e:
        logger.exception("[mineru] HTTP 请求异常")
        return ToolChunk(text=f"MinerU 网络异常: {e}")
    except Exception as e:
        logger.exception("[mineru] 解析异常")
        return ToolChunk(text=f"MinerU 解析异常: {e}")


# 模块级 FunctionTool 包装（同 ragflow_retrieval 范式）
mineru_parse_tool = FunctionTool(
    func=mineru_parse,
    name="mineru_parse",
    description=(
        "调用 MinerU 解析用户上传的文档（支持 pdf / docx / doc / xls / xlsx / csv / 图片），"
        "返回解析后的 Markdown 文本。适用于用户上传文件并要求提取/总结/问答文档内容的场景。"
    ),
)
