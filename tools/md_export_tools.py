"""
Markdown 导出工具（docx / pdf / xlsx / png）

将 Markdown 内容转换为 Word / PDF / Excel / PNG 文件并写入当前会话工作区根目录，
转换逻辑复用 md-exporter 包（md_exporter.services.svc_md_to_* 模块的
convert_md_to_docx / convert_md_to_pdf / convert_md_to_xlsx / convert_md_to_png），
由工厂 create_md_export_tools(read_file, write_file) 创建 4 个 FunctionTool，
闭包捕获工作区读写函数（opensandbox 分支走沙箱远程读写，docker 分支走本地读写）。
导出文件落入会话工作区后，由既有 files_generated 快照差分检测捕获，
用户可经 /files/{session_id}/{path} 下载，本模块不感知该链路。

依赖隔离：
- 不在模块顶层 import md_exporter，转换函数在工具执行体内延迟 import——
  包缺失时工具返回友好错误，不影响后端启动与 /chat 主流程。

安全性设计：
- markdown_file_path / output_file_name 均按会话工作区相对路径处理，
  越权校验规则与 app/routes/files.py 一致（禁止绝对路径与 .. 越权）。

异步与阻塞隔离：
- pandoc/typst 转换为同步阻塞子进程调用，经 asyncio.to_thread 在线程中执行，
  避免阻塞事件循环；工作区读写（opensandbox 远程调用）保持 async。
"""
import asyncio
import importlib
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, ToolChunk

logger = logging.getLogger(__name__)


def _build_result(text: str) -> ToolChunk:
    """构造 ToolChunk 结果。"""
    return ToolChunk(
        content=[TextBlock(text=text)],
        is_last=True,
    )


def _check_workspace_path(path: str, field: str) -> Optional[str]:
    """校验工作区相对路径，非法时返回错误提示文本，合法返回 None。

    规则与 app/routes/files.py 的越权校验一致：
    禁止绝对路径、.. 越权与反斜杠转义。
    """
    rel = path.replace("\\", "/").lstrip("/")
    if os.path.isabs(path) or rel.startswith("..") or "/.." in rel or rel == "..":
        return f"错误：{field} 含非法路径（禁止绝对路径或 .. 越权）：{path}"
    if not rel:
        return f"错误：{field} 不能为空。"
    return None


def _run_conversion(
    convert: Callable,
    kind: str,
    md_text: str,
    out_path: Path,
    multi_page: bool,
) -> list:
    """同步执行转换并读取产物字节（经 asyncio.to_thread 在线程中运行）。

    docx/pdf/xlsx 产物为单个 out_path；png 使用上游返回的生成文件列表
    （多页时为 stem_1.png、stem_2.png…，沿用上游编号约定）。

    Returns:
        [(文件名, 文件字节), ...]；转换函数抛出的异常原样向上传播。
    """
    if kind == "png":
        created = convert(md_text, out_path, is_multi_page=multi_page) or [out_path]
    else:
        convert(md_text, out_path)
        created = [out_path]
    return [(Path(p).name, Path(p).read_bytes()) for p in created if Path(p).is_file()]


def create_md_export_tools(
    read_file: Callable[[str], Awaitable[str]],
    write_file: Callable[[str, bytes], Awaitable[None]],
) -> list[FunctionTool]:
    """创建 Markdown 导出 FunctionTool 列表（闭包，捕获工作区读写函数）。

    产出 md_to_docx / md_to_pdf / md_to_xlsx / md_to_png 4 个工具，
    每个工具将 Markdown 内容转换为对应格式文件并写入会话工作区根目录，
    返回工作区相对路径（png 多页时返回全部文件路径列表）。

    Args:
        read_file: async (rel_path) -> str，读取会话工作区文本文件内容
        write_file: async (rel_path, bytes) -> None，向会话工作区写入二进制内容

    Returns:
        FunctionTool 实例列表
    """

    async def _run_export(
        kind: str,
        markdown_text: Optional[str],
        markdown_file_path: Optional[str],
        output_file_name: Optional[str],
        multi_page: bool = False,
    ) -> ToolChunk:
        """通用导出流程：参数校验 → 读入内容 → 临时目录转换 → 写回工作区。"""
        tool_name = f"md_to_{kind}"

        # 1. 输入二选一校验
        if markdown_text and markdown_file_path:
            return _build_result(
                "错误：markdown_text 与 markdown_file_path 只能提供其一，请仅传入其中一项。"
            )
        if not markdown_text and not markdown_file_path:
            return _build_result(
                "错误：必须提供 markdown_text（Markdown 正文文本）或 "
                "markdown_file_path（工作区内 Markdown 文件相对路径）之一。"
            )

        # 2. 读取 Markdown 内容
        if markdown_file_path:
            err = _check_workspace_path(markdown_file_path, "markdown_file_path")
            if err:
                return _build_result(err)
            try:
                md_text = await read_file(markdown_file_path)
            except Exception as e:
                logger.exception(
                    f"[{tool_name}] 读取工作区文件失败: {markdown_file_path}"
                )
                return _build_result(
                    f"错误：读取工作区文件 {markdown_file_path} 失败：{e}"
                )
        else:
            md_text = markdown_text
        if not md_text or not md_text.strip():
            return _build_result("错误：Markdown 内容为空，无法导出。")

        # 3. 确定输出文件名（统一写入工作区根目录）
        if output_file_name:
            err = _check_workspace_path(output_file_name, "output_file_name")
            if err:
                return _build_result(err)
            out_name = str(
                Path(output_file_name.replace("\\", "/")).with_suffix(f".{kind}")
            )
        elif markdown_file_path:
            # 文件输入：沿用输入文件主名（report.md -> report.docx）
            stem = Path(markdown_file_path.replace("\\", "/")).stem
            out_name = (
                f"{stem}.{kind}"
                if stem and stem != "."
                else f"export_{datetime.now():%Y%m%d_%H%M%S}.{kind}"
            )
        else:
            # 文本输入：默认时间戳名
            out_name = f"export_{datetime.now():%Y%m%d_%H%M%S}.{kind}"

        # 4. 延迟 import md_exporter 转换函数（包缺失时返回友好错误）
        try:
            module = importlib.import_module(
                f"md_exporter.services.svc_md_to_{kind}"
            )
            convert = getattr(module, f"convert_md_to_{kind}")
        except ImportError as e:
            logger.warning(f"[{tool_name}] md_exporter 不可用: {e}")
            return _build_result(
                f"错误：md-exporter 依赖不可用（{e}），无法执行 {kind} 导出，"
                "请联系管理员安装 md-exporter>=4.0.0。"
            )

        # 5. 临时目录中执行转换（阻塞调用放线程），产物字节写回工作区
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                out_path = Path(tmp_dir) / out_name
                try:
                    files = await asyncio.to_thread(
                        _run_conversion, convert, kind, md_text, out_path, multi_page
                    )
                except Exception as e:
                    logger.exception(f"[{tool_name}] 转换失败 out={out_name}")
                    return _build_result(f"错误：Markdown 转换为 {kind} 失败：{e}")

                if not files:
                    return _build_result(
                        f"错误：Markdown 转换为 {kind} 失败：未生成任何输出文件。"
                    )

                try:
                    for file_name, data in files:
                        await write_file(file_name, data)
                except Exception as e:
                    logger.exception(f"[{tool_name}] 写入工作区失败 out={out_name}")
                    return _build_result(f"错误：导出文件写入工作区失败：{e}")
        except Exception as e:
            logger.exception(f"[{tool_name}] 导出流程异常 out={out_name}")
            return _build_result(f"错误：导出 {kind} 文件失败：{e}")

        # 6. 返回工作区相对路径（png 多页全量列出）
        if len(files) == 1:
            return _build_result(f"已成功导出 {kind} 文件：{files[0][0]}")
        return _build_result(
            f"已成功导出 {len(files)} 个 {kind} 文件：\n"
            + "\n".join(f"- {file_name}" for file_name, _ in files)
        )

    async def md_to_docx(
        markdown_text: Optional[str] = None,
        markdown_file_path: Optional[str] = None,
        output_file_name: Optional[str] = None,
    ) -> ToolChunk:
        """将 Markdown 内容转换为 Word 文档（.docx），写入当前会话工作区根目录。

        markdown_text 与 markdown_file_path 必须且只能提供其一：
        前者为 Markdown 正文文本，后者为工作区内 Markdown 文件的相对路径
        （例如先用 Write 工具落盘的 report.md）。
        成功后返回导出文件的工作区相对路径。

        Args:
            markdown_text: Markdown 正文文本（与 markdown_file_path 二选一）
            markdown_file_path: 工作区内 Markdown 文件相对路径（与 markdown_text 二选一）
            output_file_name: 输出文件名（可选），默认取输入文件主名或 export_时间戳，扩展名 .docx
        """
        return await _run_export(
            "docx", markdown_text, markdown_file_path, output_file_name
        )

    async def md_to_pdf(
        markdown_text: Optional[str] = None,
        markdown_file_path: Optional[str] = None,
        output_file_name: Optional[str] = None,
    ) -> ToolChunk:
        """将 Markdown 内容转换为 PDF 文档（.pdf），写入当前会话工作区根目录。

        markdown_text 与 markdown_file_path 必须且只能提供其一：
        前者为 Markdown 正文文本，后者为工作区内 Markdown 文件的相对路径
        （例如先用 Write 工具落盘的 report.md）。
        成功后返回导出文件的工作区相对路径。

        Args:
            markdown_text: Markdown 正文文本（与 markdown_file_path 二选一）
            markdown_file_path: 工作区内 Markdown 文件相对路径（与 markdown_text 二选一）
            output_file_name: 输出文件名（可选），默认取输入文件主名或 export_时间戳，扩展名 .pdf
        """
        return await _run_export(
            "pdf", markdown_text, markdown_file_path, output_file_name
        )

    async def md_to_xlsx(
        markdown_text: Optional[str] = None,
        markdown_file_path: Optional[str] = None,
        output_file_name: Optional[str] = None,
    ) -> ToolChunk:
        """将 Markdown 内容中的表格转换为 Excel 工作簿（.xlsx），写入当前会话工作区根目录。

        输入需包含合法的 Markdown 表格语法，每个 Markdown 表格导出为一个 sheet。
        markdown_text 与 markdown_file_path 必须且只能提供其一：
        前者为 Markdown 正文文本，后者为工作区内 Markdown 文件的相对路径
        （例如先用 Write 工具落盘的 report.md）。
        成功后返回导出文件的工作区相对路径。

        Args:
            markdown_text: Markdown 正文文本（与 markdown_file_path 二选一）
            markdown_file_path: 工作区内 Markdown 文件相对路径（与 markdown_text 二选一）
            output_file_name: 输出文件名（可选），默认取输入文件主名或 export_时间戳，扩展名 .xlsx
        """
        return await _run_export(
            "xlsx", markdown_text, markdown_file_path, output_file_name
        )

    async def md_to_png(
        markdown_text: Optional[str] = None,
        markdown_file_path: Optional[str] = None,
        output_file_name: Optional[str] = None,
        multi_page: bool = False,
    ) -> ToolChunk:
        """将 Markdown 内容渲染为 PNG 图片，写入当前会话工作区根目录。

        markdown_text 与 markdown_file_path 必须且只能提供其一：
        前者为 Markdown 正文文本，后者为工作区内 Markdown 文件的相对路径
        （例如先用 Write 工具落盘的 report.md）。
        multi_page=True 时按 A4 分页导出，每页一个编号文件
        （name_1.png、name_2.png…），返回全部文件路径；
        默认整篇渲染为单张长图。成功后返回导出文件的工作区相对路径。

        Args:
            markdown_text: Markdown 正文文本（与 markdown_file_path 二选一）
            markdown_file_path: 工作区内 Markdown 文件相对路径（与 markdown_text 二选一）
            output_file_name: 输出文件名（可选），默认取输入文件主名或 export_时间戳，扩展名 .png
            multi_page: 是否按 A4 分页导出多张图片，默认 False（单张长图）
        """
        return await _run_export(
            "png", markdown_text, markdown_file_path, output_file_name, multi_page
        )

    return [
        FunctionTool(
            func=md_to_docx,
            name="md_to_docx",
            description=(
                "将 Markdown 内容转换为 Word 文档（.docx）并写入当前会话工作区，返回工作区相对路径。"
                "当用户需要把 Markdown 文本、报告导出为 Word 文件时调用。"
                "支持直接传入 Markdown 文本，或指定工作区内 .md 文件路径（先用 Write 落盘再导出）。"
            ),
        ),
        FunctionTool(
            func=md_to_pdf,
            name="md_to_pdf",
            description=(
                "将 Markdown 内容转换为 PDF 文档（.pdf）并写入当前会话工作区，返回工作区相对路径。"
                "当用户需要把 Markdown 文本、报告导出为 PDF 文件时调用。"
                "支持直接传入 Markdown 文本，或指定工作区内 .md 文件路径（先用 Write 落盘再导出）。"
            ),
        ),
        FunctionTool(
            func=md_to_xlsx,
            name="md_to_xlsx",
            description=(
                "将 Markdown 内容中的表格转换为 Excel 工作簿（.xlsx）并写入当前会话工作区，"
                "每个 Markdown 表格导出为一个 sheet，返回工作区相对路径。"
                "当用户需要把 Markdown 表格数据导出为 Excel 文件时调用。"
                "支持直接传入 Markdown 文本，或指定工作区内 .md 文件路径（先用 Write 落盘再导出）。"
            ),
        ),
        FunctionTool(
            func=md_to_png,
            name="md_to_png",
            description=(
                "将 Markdown 内容渲染为 PNG 图片并写入当前会话工作区，返回工作区相对路径；"
                "multi_page=True 时按 A4 分页导出多张编号图片并返回全部路径。"
                "当用户需要把 Markdown 文本、报告导出为图片时调用。"
                "支持直接传入 Markdown 文本，或指定工作区内 .md 文件路径（先用 Write 落盘再导出）。"
            ),
        ),
    ]
