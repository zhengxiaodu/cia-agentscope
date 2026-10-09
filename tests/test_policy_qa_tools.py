"""policy_qa 工具 _build_result 单测：citations 写入 ToolChunk.metadata。"""
from tools.policy_qa_tools import _build_result


def test_build_result_with_citations():
    """带 citations 时应写入 ToolChunk.metadata['citations']。"""
    cites = [{"position": 1, "document_name": "docA", "content": "..."}]
    chunk = _build_result("answer text", citations=cites)
    assert chunk.is_last is True
    assert chunk.content[0].text == "answer text"
    assert chunk.metadata.get("citations") == cites


def test_build_result_without_citations():
    """无 citations 时 metadata 应为空 dict（不含 citations 键）。"""
    chunk = _build_result("no refs")
    assert chunk.metadata == {}


def test_build_result_citations_none():
    """显式传 None 时与不传一致：metadata 为空 dict。"""
    chunk = _build_result("none case", citations=None)
    assert chunk.metadata == {}
