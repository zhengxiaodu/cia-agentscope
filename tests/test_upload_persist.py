"""上传文件解析内容落沙箱（persist_upload_files_to_workspace）测试。

覆盖：成功写入返回路径列表、写失败降级跳过、无依赖静默返回空、
load_upload_context 两种就近声明分支（写入成功/未写入）。
全部纯 mock（DAO 与 workspace_manager 均为内存假对象），无任何真实连接。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.upload_context_provider import (
    load_upload_context,
    persist_upload_files_to_workspace,
)
from app.services.file_change_detector import diff, filter_allowed, is_allowed_file


def test_diff_excludes_upload_prefix():
    """uploads/ 是平台写入目录（before 快照后写入），不得判为模型产物。"""
    before = {"skills/policy_qa/SKILL.md"}
    after = before | {
        "uploads/报表.xlsx.md",      # persist 写入的解析文本
        "报告.docx",                  # 模型真实产物
    }
    assert diff(before, after) == ["uploads/报表.xlsx.md", "报告.docx"]  # 不传排除时全保留
    assert diff(before, after, exclude_prefixes=("uploads/",)) == ["报告.docx"]
    # 边界：前缀 uploads/ 不误伤 uploads_report.docx 这类正常文件名
    after2 = after | {"uploads_v2.docx"}
    assert diff(before, after2, exclude_prefixes=("uploads/",)) == [
        "uploads_v2.docx", "报告.docx"
    ]


def test_diff_dict_signature_detects_overwrite():
    """带签名快照（dict）：同路径文件 mtime/size 变化（重生成/覆盖）
    也算本轮产物；签名未变的旧文件不算。"""
    before = {
        "报告.docx": ("1770000000.0000000000", "1024"),
        "数据.xlsx": ("1770000000.0000000000", "2048"),
    }
    after = {
        # 覆盖重生成：mtime 与 size 都变了
        "报告.docx": ("1770009999.1234567890", "2048"),
        # 未变化
        "数据.xlsx": ("1770000000.0000000000", "2048"),
        # 新增
        "图.png": ("1770008888.0000000000", "512"),
    }
    assert diff(before, after) == ["图.png", "报告.docx"]
    # 排除前缀对 dict 快照同样生效
    after["uploads/表格.csv.md"] = ("1770007777.0", "10")
    assert diff(before, after, exclude_prefixes=("uploads/",)) == ["图.png", "报告.docx"]


def test_diff_mixed_set_and_dict_falls_back_to_paths():
    """set 与 dict 混用（如 mock 与真实快照混用）时退化为纯路径差集，
    不误报签名变化。"""
    before = {"报告.docx", "数据.xlsx"}
    after = {
        "报告.docx": ("1770009999.0", "2048"),  # 签名不同但路径已存在
        "图.png": ("1770008888.0", "512"),
    }
    assert diff(before, after) == ["图.png"]


def test_filter_allowed_extensions():
    """白名单过滤：常规文件与指定代码文件保留，svg/log 等过程文件屏蔽。"""
    paths = [
        "报告.docx", "分析.pdf", "总结.md", "数据.xlsx", "演示.pptx",
        "chart.svg", "run.log", "data.json", "README",
        "web/index.HTML", "web/style.css", "web/app.JS", "script.py",
    ]
    # 保持输入顺序（排序由 diff 负责）
    assert filter_allowed(paths) == [
        "报告.docx", "分析.pdf", "总结.md", "数据.xlsx", "演示.pptx",
        "web/index.HTML", "web/style.css", "web/app.JS", "script.py",
    ]
    # 扩展名大小写不敏感
    assert is_allowed_file("Report.PDF") is True
    assert is_allowed_file("CHART.SVG") is False
    # 无扩展名 / 隐藏文件（splitext 视为无扩展名）不允许
    assert is_allowed_file("Makefile") is False
    assert is_allowed_file(".env") is False
    # 保留子目录路径，保持原顺序
    assert filter_allowed(["b/图.png", "a/x.log", "b/a.png"]) == ["b/图.png", "b/a.png"]


def _make_request(rows):
    """构造带 upload_file_dao 的假 request（load_unbound_parsed 返回 rows）。"""
    dao = MagicMock()
    dao.load_unbound_parsed = AsyncMock(return_value=rows)
    dao.load_unbound_parsing = AsyncMock(return_value=[])
    dao.load_all_parsed = AsyncMock(return_value=[])
    state = SimpleNamespace(upload_file_dao=dao)
    request = MagicMock()
    request.app = SimpleNamespace(state=state)
    return request


class _FakeRow(dict):
    def get(self, key, default=None):  # dict.get 语义即可
        return super().get(key, default)


def _row(filename: str, content: str, fid: int = 1) -> _FakeRow:
    return _FakeRow(id=fid, filename=filename, parsed_content=content)


class _FakeWorkspaceManager:
    """write_session_file 可控成败的假沙箱管理器。"""

    def __init__(self, fail_names=(), existing=None):
        self.calls = []
        self.fail_names = set(fail_names)
        self.existing = set(existing or [])

    async def list_session_files(self, user_id, session_id):
        return set(self.existing)

    async def write_session_file(self, user_id, session_id, rel, content):
        self.calls.append((user_id, session_id, rel, content))
        return not any(n in rel for n in self.fail_names)


@pytest.mark.asyncio
async def test_persist_writes_all_parsed_files():
    request = _make_request([
        _row("报表.xlsx", "# 表格数据", fid=1),
        _row("说明.pdf", "# 说明文档", fid=2),
    ])
    mgr = _FakeWorkspaceManager()
    result = await persist_upload_files_to_workspace(
        request, "sess-1", "user-1", mgr
    )
    assert sorted(result["sandbox_files"]) == [
        "uploads/报表.xlsx.md", "uploads/说明.pdf.md"
    ]
    assert result["file_ids"] == [1, 2]
    assert len(mgr.calls) == 2
    # 写入内容为完整 parsed_content
    assert mgr.calls[0][3] == "# 表格数据"


@pytest.mark.asyncio
async def test_persist_skips_failed_writes_and_empty_content():
    request = _make_request([
        _row("a.pdf", "内容A", fid=1),
        _row("b.pdf", "内容B", fid=2),
        _row("空.pdf", ""),  # 空内容跳过
    ])
    mgr = _FakeWorkspaceManager(fail_names=("b.pdf",))
    result = await persist_upload_files_to_workspace(
        request, "sess-1", "user-1", mgr
    )
    # 写失败的 b.pdf 不在 sandbox_files，但已消费（注入了文本），file_ids 包含
    assert result["sandbox_files"] == ["uploads/a.pdf.md"]
    assert result["file_ids"] == [1, 2]
    assert len(mgr.calls) == 2  # 空内容不调用写入


@pytest.mark.asyncio
async def test_persist_heals_missing_bound_files():
    """沙箱自愈：已绑定但沙箱中缺失的历史解析文件被补写，已存在的不重写。"""
    request = _make_request([])  # 本轮无未消费文件
    request.app.state.upload_file_dao.load_all_parsed = AsyncMock(return_value=[
        {"filename": "旧报告.pdf", "parsed_content": "# 旧内容", "message_id": 55},
        {"filename": "仍在.pdf", "parsed_content": "# 仍在", "message_id": 56},
    ])
    mgr = _FakeWorkspaceManager(existing={"uploads/仍在.pdf.md"})
    result = await persist_upload_files_to_workspace(request, "sess-1", "user-1", mgr)
    assert result["sandbox_files"] == []
    assert result["file_ids"] == []
    # 仅补写缺失的旧报告；仍在.pdf 未被重写
    assert [c[2] for c in mgr.calls] == ["uploads/旧报告.pdf.md"]
    assert mgr.calls[0][3] == "# 旧内容"


@pytest.mark.asyncio
async def test_persist_missing_dependency_returns_empty():
    request = _make_request([_row("a.pdf", "内容")])
    empty = {"file_ids": [], "sandbox_files": []}
    # workspace_manager 为 None → 静默降级返回空
    assert await persist_upload_files_to_workspace(
        request, "sess-1", "user-1", None
    ) == empty
    # request 为 None → 空
    assert await persist_upload_files_to_workspace(
        None, "sess-1", "user-1", _FakeWorkspaceManager()
    ) == empty


@pytest.mark.asyncio
async def test_load_context_declaration_branches():
    request = _make_request([_row("报表.xlsx", "# 数据")])
    # 写入成功分支：声明"已同步保存到沙箱"
    ctx_ok = await load_upload_context(request, "sess-1", ["uploads/报表.xlsx.md"])
    assert "已同步保存到沙箱工作目录" in ctx_ok
    assert "不要向用户表示找不到文件" in ctx_ok
    assert "=== 文件名: 报表.xlsx ===" in ctx_ok
    # 未写入分支：声明"不在沙箱勿查找"
    ctx_fallback = await load_upload_context(request, "sess-1")
    assert "均不在沙箱文件系统中" in ctx_fallback
    assert "不要尝试在沙箱中查找本轮新上传的文件" in ctx_fallback
    # 无上传文件：返回空串
    empty_request = _make_request([])
    assert await load_upload_context(empty_request, "sess-1") == ""
