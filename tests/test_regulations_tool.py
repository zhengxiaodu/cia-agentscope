"""policy_qa 工具（进程内调用）单测：权限解析 / kb_ids 映射 / 知识库选择过滤 / 服务调用 / 各类兜底文案。"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.regulations.runtime as runtime_module
import tools.policy_qa_tools as tool_module
from app.regulations.schemas import GeneralQAResponse, RetrieverResource
from tools.policy_qa_tools import (
    _format_citations,
    _resolve_kb_ids,
    create_policy_qa_tool,
    resolve_kb_ids,
    resolve_user_kb_names,
)


@pytest.fixture(autouse=True)
def _isolate_kb_map(monkeypatch):
    """屏蔽环境变量 POLICY_QA_KB_MAP。

    内网 .env 通常配置真实 kb_id（UUID 格式）的映射 JSON，会污染用例里
    基于 _DEFAULT_PERMISSION_KB_MAP（"123"/"456"）的期望值。置空后
    _load_permission_kb_map 回退到代码内默认映射，测试对任意环境免疫。
    """
    monkeypatch.setattr(tool_module, "POLICY_QA_KB_MAP", "")


def _permissions_json(names):
    return {"permissions": {"agent_whitelist": [{"name": n} for n in names]}}


def _qa_response(answer="报销标准如下", with_citation=True):
    citations = [
        RetrieverResource(
            position=1, dataset_id="123", dataset_name="金科制度库",
            document_id="d1", document_name="差旅报销办法", segment_id="s1",
            score=0.9, page_start=1, page_end=2,
        ),
    ] if with_citation else []
    return GeneralQAResponse(
        answer=answer, citations=citations,
        app_serial_number="serial-1", model="test-model", created_at=0,
    )


async def _call_tool(monkeypatch, *, perms=None, perms_error=None,
                     service=None, service_error=None, service_not_ready=False,
                     question="差旅报销标准是什么？", knowledge_bases=None):
    """构造工具并调用，返回 ToolChunk。"""
    if perms_error is not None:
        monkeypatch.setattr(
            tool_module, "get_user_permissions",
            AsyncMock(side_effect=perms_error),
        )
    else:
        monkeypatch.setattr(
            tool_module, "get_user_permissions",
            AsyncMock(return_value=perms),
        )

    if service_not_ready:
        def _raise():
            raise RuntimeError("制度问答服务未初始化")
        monkeypatch.setattr(runtime_module, "get_policy_qa_service", _raise)
    else:
        monkeypatch.setattr(
            runtime_module, "get_policy_qa_service", lambda: service,
        )

    tool = create_policy_qa_tool(
        "u1", redis_client=MagicMock(), knowledge_bases=knowledge_bases,
    )
    return await tool.call(question=question)


def _text(chunk) -> str:
    return chunk.content[0].text


# ---- 权限解析纯函数 ----

def test_resolve_kb_ids_maps_and_dedups():
    """权限名 → kb_id 映射、去重、保持插入顺序；无关权限忽略。"""
    perms = _permissions_json(["金科制度问答", "无关权限", "信科制度问答", "金科制度问答"])
    assert _resolve_kb_ids(perms["permissions"]) == ["123", "456"]


def test_resolve_kb_ids_empty_inputs():
    assert _resolve_kb_ids(None) == []
    assert _resolve_kb_ids({}) == []
    assert _resolve_kb_ids({"agent_whitelist": "not-a-list"}) == []
    assert _resolve_kb_ids({"agent_whitelist": [{"no_name": 1}, "bad"]}) == []


# ---- resolve_kb_ids 公共入口（权限解析 + knowledge_bases 过滤）----

def test_resolve_kb_ids_public_full_scope():
    """knowledge_bases 为 None/空 → 全选语义，等价 _resolve_kb_ids。"""
    perms = _permissions_json(["金科制度问答", "信科制度问答"])["permissions"]
    assert resolve_kb_ids(perms) == ["123", "456"]
    assert resolve_kb_ids(perms, None) == ["123", "456"]
    assert resolve_kb_ids(perms, []) == ["123", "456"]
    assert resolve_kb_ids(None, None) == []


def test_resolve_kb_ids_public_selected_subset():
    """选择部分库 → 交集，保持权限插入顺序。"""
    perms = _permissions_json(["金科制度问答", "信科制度问答"])["permissions"]
    assert resolve_kb_ids(perms, ["信科制度问答"]) == ["456"]
    assert resolve_kb_ids(perms, ["信科制度问答", "金科制度问答"]) == ["123", "456"]


def test_resolve_kb_ids_public_ignores_invalid_and_empty_intersection():
    """混入无效名静默忽略；全部无效 → 空列表。"""
    perms = _permissions_json(["金科制度问答", "信科制度问答"])["permissions"]
    assert resolve_kb_ids(perms, ["金科制度问答", "不存在的库"]) == ["123"]
    assert resolve_kb_ids(perms, ["不存在的库"]) == []
    assert resolve_kb_ids(None, ["金科制度问答"]) == []


def test_resolve_user_kb_names():
    """下发前端的知识库名列表 = 权限名（去重、保序）。"""
    perms = _permissions_json(
        ["金科制度问答", "无关权限", "信科制度问答", "金科制度问答"],
    )["permissions"]
    assert resolve_user_kb_names(perms) == ["金科制度问答", "信科制度问答"]
    assert resolve_user_kb_names(None) == []


# ---- 引用格式化 ----

def test_format_citations():
    text = _format_citations([
        {"position": 1, "document_name": "差旅办法", "page_start": 1, "page_end": 2,
         "dataset_name": "金科库"},
        {"position": 2, "document_name": "考勤规范", "page_start": 3, "page_end": None,
         "dataset_name": ""},
    ])
    lines = text.splitlines()
    assert lines[0] == "[1] 差旅办法 p1-2 [金科库]"
    assert lines[1] == "[2] 考勤规范 p3"


def test_format_citations_empty():
    assert _format_citations([]) == ""
    assert _format_citations(["not-a-dict"]) == ""


# ---- 工具主流程 ----

@pytest.mark.asyncio
async def test_tool_with_permission_calls_local_service(monkeypatch):
    """有权限 → 调用本地服务（kb_ids 由权限映射、user_id 透传），拼接引用来源。"""
    service = MagicMock()
    service.run_general_qa = AsyncMock(return_value=_qa_response())

    chunk = await _call_tool(
        monkeypatch,
        perms=_permissions_json(["金科制度问答"]),
        service=service,
    )

    # 服务收到正确参数：kb_ids 映射为 "123"，user_id/user_department 透传
    service.run_general_qa.assert_awaited_once_with(
        question="差旅报销标准是什么？", kb_ids=["123"],
        user_id="u1", user_department="",
    )

    # 返回文本：【制度问答回答】标记包裹正文 + 引用来源 + 直接输出指令
    text = _text(chunk)
    assert text.startswith("【制度问答回答】")
    assert "报销标准如下" in text
    assert "--- 引用来源 ---" in text
    assert "[1] 差旅报销办法 p1-2 [金科制度库]" in text
    assert "请直接输出【制度问答回答】部分的内容作为最终回答" in text

    # citations 写入 metadata（由事件层透传）
    assert chunk.is_last is True
    assert chunk.metadata["citations"][0]["document_name"] == "差旅报销办法"


@pytest.mark.asyncio
async def test_tool_without_citation_no_source_section(monkeypatch):
    """无引用时正文后不拼接引用来源段落。"""
    service = MagicMock()
    service.run_general_qa = AsyncMock(return_value=_qa_response(with_citation=False))

    chunk = await _call_tool(
        monkeypatch, perms=_permissions_json(["金科制度问答"]), service=service,
    )
    # 无引用时仍包裹【制度问答回答】标记，但不拼接引用来源段落
    assert _text(chunk) == "【制度问答回答】\n报销标准如下"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_tool_no_permission_cached_missing(monkeypatch):
    """Redis 无权限缓存（返回 None）→ 无权限文案，不调服务。"""
    service = MagicMock()
    chunk = await _call_tool(monkeypatch, perms=None, service=service)

    assert _text(chunk) == "您没有制度问答权限，请联系管理员开通。"
    service.run_general_qa.assert_not_called()


@pytest.mark.asyncio
async def test_tool_no_permission_name_not_matched(monkeypatch):
    """权限缓存存在但白名单无匹配权限名 → 无权限文案。"""
    service = MagicMock()
    chunk = await _call_tool(
        monkeypatch, perms=_permissions_json(["别的智能体"]), service=service,
    )
    assert _text(chunk) == "您没有制度问答权限，请联系管理员开通。"
    service.run_general_qa.assert_not_called()


@pytest.mark.asyncio
async def test_tool_redis_error_falls_back(monkeypatch):
    """读权限异常 → 无权限文案（不区分网络失败与无权限）。"""
    chunk = await _call_tool(
        monkeypatch, perms_error=RuntimeError("redis down"), service=MagicMock(),
    )
    assert _text(chunk) == "您没有制度问答权限，请联系管理员开通。"


@pytest.mark.asyncio
async def test_tool_empty_question_rejected(monkeypatch):
    """空问题直接拒绝，不查权限。"""
    monkeypatch.setattr(
        tool_module, "get_user_permissions",
        AsyncMock(side_effect=AssertionError("不应读权限")),
    )
    tool = create_policy_qa_tool("u1", redis_client=MagicMock())
    chunk = await tool.call(question="   ")
    assert _text(chunk) == "错误：问题不能为空。"


@pytest.mark.asyncio
async def test_tool_missing_user_id_or_redis(monkeypatch):
    """缺 user_id 或 redis_client → 无权限文案。"""
    tool = create_policy_qa_tool("", redis_client=MagicMock())
    chunk = await tool.call(question="q")
    assert _text(chunk) == "您没有制度问答权限，请联系管理员开通。"

    tool2 = create_policy_qa_tool("u1", redis_client=None)
    chunk2 = await tool2.call(question="q")
    assert _text(chunk2) == "您没有制度问答权限，请联系管理员开通。"


@pytest.mark.asyncio
async def test_tool_service_not_ready(monkeypatch):
    """服务未初始化（RuntimeError）→ 兜底文案。"""
    chunk = await _call_tool(
        monkeypatch,
        perms=_permissions_json(["金科制度问答"]),
        service_not_ready=True,
    )
    assert _text(chunk) == "制度问答服务暂不可用，请稍后重试。"


@pytest.mark.asyncio
async def test_tool_service_exception_falls_back(monkeypatch):
    """服务调用抛异常 → 兜底文案（不向模型抛错）。"""
    service = MagicMock()
    service.run_general_qa = AsyncMock(side_effect=RuntimeError("kb down"))

    chunk = await _call_tool(
        monkeypatch, perms=_permissions_json(["金科制度问答"]), service=service,
    )
    assert _text(chunk).startswith("制度问答调用异常:")
    assert "kb down" in _text(chunk)


@pytest.mark.asyncio
async def test_tool_empty_answer_placeholder(monkeypatch):
    """服务返回空 answer → 占位文案。"""
    service = MagicMock()
    service.run_general_qa = AsyncMock(return_value=_qa_response(answer=""))

    chunk = await _call_tool(
        monkeypatch, perms=_permissions_json(["金科制度问答"]), service=service,
    )
    assert _text(chunk) == "未检索到相关内容，无法回答。"


# ---- 知识库选择（knowledge_bases 闭包过滤）----

@pytest.mark.asyncio
async def test_tool_kb_selection_filters_kb_ids(monkeypatch):
    """选择单个库 → 服务只收到该库 kb_ids；未选库仍在权限内但被过滤。"""
    service = MagicMock()
    service.run_general_qa = AsyncMock(return_value=_qa_response())

    chunk = await _call_tool(
        monkeypatch,
        perms=_permissions_json(["金科制度问答", "信科制度问答"]),
        service=service,
        knowledge_bases=["信科制度问答"],
    )
    service.run_general_qa.assert_awaited_once_with(
        question="差旅报销标准是什么？", kb_ids=["456"],
        user_id="u1", user_department="",
    )
    assert _text(chunk).startswith("【制度问答回答】")


@pytest.mark.asyncio
async def test_tool_kb_selection_invalid_names_ignored(monkeypatch):
    """选择混入无效名 → 静默忽略无效项，用交集执行。"""
    service = MagicMock()
    service.run_general_qa = AsyncMock(return_value=_qa_response())

    await _call_tool(
        monkeypatch,
        perms=_permissions_json(["金科制度问答", "信科制度问答"]),
        service=service,
        knowledge_bases=["金科制度问答", "不存在的库"],
    )
    assert service.run_general_qa.await_args.kwargs["kb_ids"] == ["123"]


@pytest.mark.asyncio
async def test_tool_kb_selection_all_invalid_prompts_relogin(monkeypatch):
    """所选全部不在权限内 → 引导重新登录文案，不调服务。"""
    service = MagicMock()
    chunk = await _call_tool(
        monkeypatch,
        perms=_permissions_json(["金科制度问答"]),
        service=service,
        knowledge_bases=["不存在的库"],
    )
    assert _text(chunk) == "您没有所选知识库的访问权限，请退出重新登录后重试。"
    service.run_general_qa.assert_not_called()


@pytest.mark.asyncio
async def test_tool_kb_selection_empty_keeps_full_scope(monkeypatch):
    """knowledge_bases 为空列表 → 全选（与现状一致），兼容不带新字段的前端。"""
    service = MagicMock()
    service.run_general_qa = AsyncMock(return_value=_qa_response())

    await _call_tool(
        monkeypatch,
        perms=_permissions_json(["金科制度问答", "信科制度问答"]),
        service=service,
        knowledge_bases=[],
    )
    assert service.run_general_qa.await_args.kwargs["kb_ids"] == ["123", "456"]


@pytest.mark.asyncio
async def test_tool_no_selection_no_permission_keeps_old_copy(monkeypatch):
    """未做选择且无权限 → 保持原有"请联系管理员开通"文案。"""
    service = MagicMock()
    chunk = await _call_tool(
        monkeypatch, perms=_permissions_json(["别的智能体"]), service=service,
    )
    assert _text(chunk) == "您没有制度问答权限，请联系管理员开通。"
    service.run_general_qa.assert_not_called()
