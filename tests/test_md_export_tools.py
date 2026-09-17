"""tools/md_export_tools.py 单测。

覆盖（对应 .trae/specs/add-markdown-export-tools checklist）：
- 参数二选一校验：双空 / 双传 / 内容为空，均不触发转换与写入
- 路径越权防护：markdown_file_path 与 output_file_name 的绝对路径、..、反斜杠转义
- 读写接线：read_file / write_file 闭包注入、默认命名（输入主名 / export_时间戳）、
  output_file_name 后缀强制替换、临时目录转换与自动清理
- png 多页：全量写回与列表返回、multi_page 透传、上游空列表回退单文件
- 异常降级：ImportError / 转换抛错 / 未生成产物 / 读文件失败 / 写文件失败
- xlsx 真实转换冒烟（md-exporter 缺失时 skip）

docx/pdf/xlsx/png 的转换通过向 sys.modules 注入伪造 md_exporter 模块 mock，
不依赖真实 pandoc/typst 子进程。
"""
import io
import re
import sys
import types
import zipfile
from pathlib import Path

import pytest

from tools.md_export_tools import _check_workspace_path, create_md_export_tools

MD_TEXT = "# 标题\n\n正文段落。\n"

PNG_PAYLOAD = b"PNG-BYTES"


class _FakeWorkspace:
    """记录 read_file / write_file 调用的假会话工作区。"""

    def __init__(self, content: str = MD_TEXT):
        self.content = content
        self.reads: list[str] = []
        self.writes: list[tuple[str, bytes]] = []
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None

    async def read_file(self, path: str) -> str:
        self.reads.append(path)
        if self.read_error:
            raise self.read_error
        return self.content

    async def write_file(self, name: str, data: bytes) -> None:
        if self.write_error:
            raise self.write_error
        self.writes.append((name, data))


def _chunk_text(chunk) -> str:
    """提取 ToolChunk 首个文本块内容。"""
    return chunk.content[0].text


def _make_tool(kind: str, ws: _FakeWorkspace):
    """创建指定导出工具（闭包捕获 ws 的读写函数）。"""
    tools = {
        t.name: t for t in create_md_export_tools(ws.read_file, ws.write_file)
    }
    return tools[f"md_to_{kind}"]


def _install_fake_converter(monkeypatch, kind: str, convert) -> list[dict]:
    """向 sys.modules 注入伪造的 md_exporter 转换模块，返回调用记录列表。"""
    calls: list[dict] = []

    def wrapped(md_text, out_path, **kwargs):
        calls.append({"md_text": md_text, "out_path": Path(out_path), **kwargs})
        return convert(md_text, Path(out_path), **kwargs)

    module = types.ModuleType(f"md_exporter.services.svc_md_to_{kind}")
    setattr(module, f"convert_md_to_{kind}", wrapped)
    monkeypatch.setitem(sys.modules, f"md_exporter.services.svc_md_to_{kind}", module)
    return calls


def _single_file_converter(payload: bytes = b"FAKE-OUTPUT"):
    """docx/pdf/xlsx 语义：向 out_path 写出单个产物文件。"""

    def convert(md_text, out_path, **kwargs):
        out_path.write_bytes(payload)

    return convert


def _png_converter(pages: int = 0):
    """png 语义：多页时生成 stem_N.png 列表，否则单文件。"""

    def convert(md_text, out_path, **kwargs):
        if pages <= 0:
            out_path.write_bytes(PNG_PAYLOAD)
            return [out_path]
        created = []
        for i in range(1, pages + 1):
            p = out_path.with_name(f"{out_path.stem}_{i}{out_path.suffix}")
            p.write_bytes(PNG_PAYLOAD + str(i).encode())
            created.append(p)
        return created

    return convert


# ---------- 工厂与工具面 ----------


def test_create_returns_four_named_tools():
    """工厂应产出 4 个命名正确、描述非空的 FunctionTool。"""
    ws = _FakeWorkspace()
    tools = create_md_export_tools(ws.read_file, ws.write_file)
    assert [t.name for t in tools] == ["md_to_docx", "md_to_pdf", "md_to_xlsx", "md_to_png"]
    for tool in tools:
        assert tool.description, f"{tool.name} 描述不应为空"


# ---------- 参数二选一校验 ----------


@pytest.mark.asyncio
async def test_neither_input_rejected():
    """双空：必须提供 markdown_text 或 markdown_file_path 之一。"""
    ws = _FakeWorkspace()
    tool = _make_tool("docx", ws)
    chunk = await tool(markdown_text=None, markdown_file_path=None)
    text = _chunk_text(chunk)
    assert "必须提供" in text and "markdown_file_path" in text
    assert ws.reads == [] and ws.writes == []


@pytest.mark.asyncio
async def test_both_inputs_rejected():
    """双传：只能提供其一，且不读取、不转换。"""
    ws = _FakeWorkspace()
    tool = _make_tool("pdf", ws)
    chunk = await tool(markdown_text="# x", markdown_file_path="a.md")
    assert "只能提供其一" in _chunk_text(chunk)
    assert ws.reads == [] and ws.writes == []


@pytest.mark.parametrize("kind", ["docx", "pdf", "xlsx", "png"])
@pytest.mark.asyncio
async def test_blank_text_rejected_without_conversion(kind, monkeypatch):
    """纯空白文本在转换前被拒，4 个工具行为一致。"""
    ws = _FakeWorkspace()
    calls = _install_fake_converter(
        monkeypatch, kind, _single_file_converter() if kind != "png" else _png_converter()
    )
    tool = _make_tool(kind, ws)
    chunk = await tool(markdown_text="   \n  ")
    assert "Markdown 内容为空" in _chunk_text(chunk)
    assert calls == [] and ws.writes == []


@pytest.mark.asyncio
async def test_file_content_blank_rejected(monkeypatch):
    """文件输入读到空白内容同样被拒，且不再走转换。"""
    ws = _FakeWorkspace(content="  \n")
    calls = _install_fake_converter(monkeypatch, "docx", _single_file_converter())
    tool = _make_tool("docx", ws)
    chunk = await tool(markdown_file_path="empty.md")
    assert "Markdown 内容为空" in _chunk_text(chunk)
    assert ws.reads == ["empty.md"]
    assert calls == [] and ws.writes == []


# ---------- 路径越权防护 ----------


@pytest.mark.parametrize(
    "path",
    ["report.md", "sub/report.md", "a/b/c.md"],
)
def test_check_workspace_path_accepts_relative(path):
    """合法工作区相对路径返回 None。"""
    assert _check_workspace_path(path, "field") is None


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "///abs.md",
        "..",
        "../up.md",
        "a/../../up.md",
        "..\\up.md",
        "a\\..\\b.md",
    ],
)
def test_check_workspace_path_rejects_escape(path):
    """绝对路径、.. 越权与反斜杠转义均被拒绝。"""
    err = _check_workspace_path(path, "field")
    assert err is not None and "非法路径" in err


def test_check_workspace_path_rejects_empty():
    """空路径返回“不能为空”。"""
    err = _check_workspace_path("", "field")
    assert err is not None and "不能为空" in err


@pytest.mark.asyncio
async def test_markdown_file_path_traversal_rejected(monkeypatch):
    """markdown_file_path 越权被拒：不读取工作区、不触发转换。"""
    ws = _FakeWorkspace()
    calls = _install_fake_converter(monkeypatch, "docx", _single_file_converter())
    tool = _make_tool("docx", ws)
    for bad in ["/etc/passwd", "../secret.md", "a/../../secret.md", "..\\secret.md"]:
        chunk = await tool(markdown_file_path=bad)
        assert "非法路径" in _chunk_text(chunk)
        assert ws.reads == []
    assert calls == [] and ws.writes == []


@pytest.mark.asyncio
async def test_output_file_name_traversal_rejected(monkeypatch):
    """output_file_name 越权被拒：不触发转换、不写入。"""
    ws = _FakeWorkspace()
    calls = _install_fake_converter(monkeypatch, "pdf", _single_file_converter())
    tool = _make_tool("pdf", ws)
    for bad in ["/tmp/evil.pdf", "../evil.pdf", "sub/../../evil.pdf", "..\\evil.pdf"]:
        chunk = await tool(markdown_text="# t", output_file_name=bad)
        assert "非法路径" in _chunk_text(chunk)
    assert calls == [] and ws.writes == []


# ---------- 读写接线 ----------


@pytest.mark.asyncio
async def test_text_input_with_output_name_end_to_end(monkeypatch):
    """文本输入 + 指定输出名：产物字节经 write_file 写入，返回工作区相对路径。"""
    ws = _FakeWorkspace()
    payload = b"PK-FAKE-DOCX"
    calls = _install_fake_converter(monkeypatch, "docx", _single_file_converter(payload))
    tool = _make_tool("docx", ws)
    chunk = await tool(markdown_text=MD_TEXT, output_file_name="我的报告.txt")

    assert chunk.is_last is True
    text = _chunk_text(chunk)
    assert "已成功导出" in text and "我的报告.docx" in text
    # 未走文件读取；后缀被强制替换为 .docx；字节原样写回工作区
    assert ws.reads == []
    assert ws.writes == [("我的报告.docx", payload)]
    assert len(calls) == 1
    assert calls[0]["md_text"] == MD_TEXT
    assert calls[0]["out_path"].name == "我的报告.docx"


@pytest.mark.asyncio
async def test_file_input_uses_input_stem(monkeypatch):
    """文件输入：经注入的 read_file 读入，默认输出沿用输入主名。"""
    ws = _FakeWorkspace(content="# report\n")
    calls = _install_fake_converter(monkeypatch, "docx", _single_file_converter())
    tool = _make_tool("docx", ws)
    chunk = await tool(markdown_file_path="report.md")

    assert ws.reads == ["report.md"]
    assert "已成功导出" in _chunk_text(chunk) and "report.docx" in _chunk_text(chunk)
    assert [name for name, _ in ws.writes] == ["report.docx"]
    assert calls[0]["md_text"] == "# report\n"


@pytest.mark.asyncio
async def test_text_input_default_timestamp_name(monkeypatch):
    """文本输入且未指定输出名：默认 export_YYYYMMDD_HHMMSS 命名。"""
    ws = _FakeWorkspace()
    _install_fake_converter(monkeypatch, "xlsx", _single_file_converter())
    tool = _make_tool("xlsx", ws)
    chunk = await tool(markdown_text=MD_TEXT)

    text = _chunk_text(chunk)
    match = re.search(r"export_\d{8}_\d{6}\.xlsx", text)
    assert match, text
    assert [name for name, _ in ws.writes] == [match.group(0)]


@pytest.mark.asyncio
async def test_conversion_runs_in_cleaned_temp_dir(monkeypatch):
    """转换在临时目录中执行且结束后自动清理。"""
    ws = _FakeWorkspace()
    calls = _install_fake_converter(monkeypatch, "docx", _single_file_converter())
    tool = _make_tool("docx", ws)
    await tool(markdown_file_path="report.md")

    tmp_dir = calls[0]["out_path"].parent
    assert not tmp_dir.exists(), "TemporaryDirectory 应已自动清理"


@pytest.mark.asyncio
async def test_read_file_failure_returns_error(monkeypatch):
    """read_file 抛错时返回包含原因的错误文本，不写入任何文件。"""
    ws = _FakeWorkspace()
    ws.read_error = RuntimeError("boom")
    _install_fake_converter(monkeypatch, "pdf", _single_file_converter())
    tool = _make_tool("pdf", ws)
    chunk = await tool(markdown_file_path="report.md")

    text = _chunk_text(chunk)
    assert "读取工作区文件 report.md 失败" in text and "boom" in text
    assert ws.writes == []


@pytest.mark.asyncio
async def test_write_file_failure_returns_error(monkeypatch):
    """write_file 抛错时返回包含原因的错误文本。"""
    ws = _FakeWorkspace()
    ws.write_error = RuntimeError("disk full")
    _install_fake_converter(monkeypatch, "docx", _single_file_converter())
    tool = _make_tool("docx", ws)
    chunk = await tool(markdown_text=MD_TEXT)

    text = _chunk_text(chunk)
    assert "导出文件写入工作区失败" in text and "disk full" in text


# ---------- 转换失败降级 ----------


@pytest.mark.asyncio
async def test_import_error_friendly_message(monkeypatch):
    """md_exporter 缺失（ImportError）时返回友好错误，不抛异常、不写工作区。"""
    ws = _FakeWorkspace()

    def _raise_import(name):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr("importlib.import_module", _raise_import)
    tool = _make_tool("docx", ws)
    chunk = await tool(markdown_text=MD_TEXT)

    text = _chunk_text(chunk)
    assert "md-exporter 依赖不可用" in text
    assert "md-exporter>=4.0.0" in text
    assert ws.writes == []


@pytest.mark.asyncio
async def test_converter_exception_returns_error(monkeypatch):
    """转换函数抛错时错误被捕获并带上原因。"""
    ws = _FakeWorkspace()

    def _raise(md_text, out_path, **kwargs):
        raise RuntimeError("pandoc crashed")

    _install_fake_converter(monkeypatch, "docx", _raise)
    tool = _make_tool("docx", ws)
    chunk = await tool(markdown_text=MD_TEXT, output_file_name="out.docx")

    text = _chunk_text(chunk)
    assert "Markdown 转换为 docx 失败" in text and "pandoc crashed" in text
    assert ws.writes == []


@pytest.mark.asyncio
async def test_no_output_file_returns_error(monkeypatch):
    """转换正常返回但未落盘任何产物文件时报错。"""
    ws = _FakeWorkspace()
    _install_fake_converter(monkeypatch, "docx", lambda md_text, out_path, **kw: None)
    tool = _make_tool("docx", ws)
    chunk = await tool(markdown_text=MD_TEXT)

    assert "未生成任何输出文件" in _chunk_text(chunk)
    assert ws.writes == []


# ---------- png 多页 ----------


@pytest.mark.asyncio
async def test_png_single_page(monkeypatch):
    """单页 png：仅写回一个文件，multi_page 默认 False 透传。"""
    ws = _FakeWorkspace()
    calls = _install_fake_converter(monkeypatch, "png", _png_converter(pages=0))
    tool = _make_tool("png", ws)
    chunk = await tool(markdown_text=MD_TEXT, output_file_name="shot.png")

    text = _chunk_text(chunk)
    assert "已成功导出 png 文件" in text and "shot.png" in text
    assert ws.writes == [("shot.png", PNG_PAYLOAD)]
    assert calls[0]["is_multi_page"] is False


@pytest.mark.asyncio
async def test_png_multi_page_lists_all_files(monkeypatch):
    """多页 png：全部文件写回工作区并在返回文本中列出，multi_page 透传为 True。"""
    ws = _FakeWorkspace()
    calls = _install_fake_converter(monkeypatch, "png", _png_converter(pages=3))
    tool = _make_tool("png", ws)
    chunk = await tool(markdown_text=MD_TEXT, output_file_name="doc.png", multi_page=True)

    text = _chunk_text(chunk)
    assert "已成功导出 3 个 png 文件" in text
    for i in (1, 2, 3):
        assert f"doc_{i}.png" in text
    assert [name for name, _ in ws.writes] == ["doc_1.png", "doc_2.png", "doc_3.png"]
    assert ws.writes[0][1] == PNG_PAYLOAD + b"1"
    assert calls[0]["is_multi_page"] is True


@pytest.mark.asyncio
async def test_png_empty_list_falls_back_to_single(monkeypatch):
    """上游返回空列表时回退按 out_path 收集单文件（or [out_path] 分支）。"""
    ws = _FakeWorkspace()

    def convert(md_text, out_path, **kwargs):
        out_path.write_bytes(PNG_PAYLOAD)
        return []

    _install_fake_converter(monkeypatch, "png", convert)
    tool = _make_tool("png", ws)
    chunk = await tool(markdown_text=MD_TEXT, output_file_name="one.png")

    assert "one.png" in _chunk_text(chunk)
    assert ws.writes == [("one.png", PNG_PAYLOAD)]


# ---------- xlsx 真实转换冒烟（md-exporter 缺失时 skip） ----------


@pytest.mark.asyncio
async def test_xlsx_real_conversion_smoke():
    """真实 md-exporter 冒烟：Markdown 两个表格 → xlsx 两个 sheet。"""
    svc = pytest.importorskip("md_exporter.services.svc_md_to_xlsx")
    assert hasattr(svc, "convert_md_to_xlsx")

    ws = _FakeWorkspace(
        content=(
            "# 标题\n\n"
            "| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 3 |\n\n"
            "| a | b |\n| - | - |\n| 1 | 2 |\n"
        )
    )
    tool = _make_tool("xlsx", ws)
    chunk = await tool(markdown_file_path="tables.md")

    assert "已成功导出" in _chunk_text(chunk) and "tables.xlsx" in _chunk_text(chunk)
    assert len(ws.writes) == 1
    name, data = ws.writes[0]
    assert name == "tables.xlsx"
    assert data[:4] == b"PK\x03\x04", "xlsx（zip）魔数校验"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "xl/workbook.xml" in z.namelist()
        assert z.read("xl/workbook.xml").count(b"<sheet ") == 2
