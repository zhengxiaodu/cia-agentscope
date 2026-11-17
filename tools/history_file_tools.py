"""历史生成文件清单与按需恢复工具（闭包绑定当前用户和会话）。"""
import json
import logging

from agentscope.tool import FunctionTool, ToolChunk
from agentscope.message import TextBlock

logger = logging.getLogger(__name__)


def create_history_file_tools(service):
    async def call(method, **kwargs):
        try:
            result = await method(**kwargs)
        except (PermissionError, ValueError) as exc:
            result = {"status": "error", "message": str(exc)}
        except Exception:
            logger.exception("[history_files] 历史文件操作失败 session=%s", service.session_id)
            result = {"status": "error", "message": "历史文件操作暂时失败，请稍后重试；不要将此错误解释为文件已丢失。"}
        try:
            payload = json.dumps(result, ensure_ascii=True)
        except (TypeError, ValueError, OverflowError, RecursionError):
            logger.exception("[history_files] 历史文件结果序列化失败 session=%s", service.session_id)
            payload = json.dumps({"status": "error", "message": "历史文件结果暂时无法读取，请重试。"})
        return ToolChunk(content=[TextBlock(text=payload)])

    async def list_history_files(query: str = "", offset: int = 0, limit: int = 50) -> ToolChunk:
        """列出当前会话历史生成文件，包括不在当前沙箱中的旧 PPTX、文档、表格和图片。

        Args:
            query: 可选文件名或路径关键词。
            offset: 分页起始位置。
            limit: 每页条数，范围 1 到 100。
        """
        return await call(service.list_files, query=query, offset=offset, limit=limit)

    async def restore_history_file(file_path: str) -> ToolChunk:
        """把当前会话历史文件的二进制原件按需恢复到工作区；已有文件不覆盖。

        用户要求继续修改历史 PPTX 等产物时，先从清单选择文件，再调用本工具。
        恢复成功后使用返回的绝对路径读取或编辑，并把修改结果另存新文件名。

        Args:
            file_path: ListHistoryFiles 返回的精确相对 path，同名文件不能只传文件名。
        """
        return await call(service.restore_file, file_path=file_path)

    return [FunctionTool(list_history_files, name="ListHistoryFiles", is_read_only=True),
            FunctionTool(restore_history_file, name="RestoreHistoryFile")]
