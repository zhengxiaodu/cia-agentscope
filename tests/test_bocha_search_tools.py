"""tools/bocha_search_tools.py 单测。

覆盖：
- 工厂：命名、描述非空、可直接 await 调用
- 入参校验：空 query / 空白 query 不发起 HTTP 请求
- 配置校验：BOCHA_API_KEY 为空时返回未配置提示，不外呼
- 成功路径：请求 url/headers/payload（query/summary/count）正确；返回文本
  含序号/标题/链接；bocha_sum 字段子集 + citation_id 写入 metadata 快照
- 请求级统一编号（citation_id）：同实例多次搜索共享连续编号、正文仅展示
  本次条目、metadata 为截至当前的完整来源表快照；重复 URL 沿用首次登记
  条目与编号；失败不占编号；早先快照不随后续登记变化；新实例从 1 重新编号
- 无效 URL 过滤：空/空白/缺失 URL 不进正文与快照、不占编号；全部无效
  视为未搜索到相关结果
- 失败路径：超时 / 网络异常 / HTTP 非 200 / 业务错误码 / 响应解析失败 /
  无搜索结果，均返回友好文本且 metadata 为空、不占编号
- 辅助函数：_extract_bocha_sum 只保留子集字段、缺失字段补空串；
  _format_search_results 编号取 citation_id 字段、尾注含引用规则文案；
  _build_result 无 bocha_sum 时 metadata 为空 dict
- merge_bocha_snapshots：按 citation_id 幂等合并（existing 优先）、
  升序输出；缺 citation_id 条目附尾部且相同 dict 去重；非 dict 跳过
"""
import httpx
import pytest

import tools.bocha_search_tools as bocha_module
from tools.bocha_search_tools import (
    _BOCHA_SUM_FIELDS,
    _build_result,
    _extract_bocha_sum,
    _format_search_results,
    create_bocha_search_tool,
    merge_bocha_snapshots,
)

# ---------------------------------------------------------------------------
# 假 httpx 客户端（记录请求，可注入响应或异常）
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


class _FakeAsyncClient:
    """替代 httpx.AsyncClient：记录 post 调用，返回预设响应或抛异常。

    支持两种模式（与既有单响应用例兼容）：
    - resp= / exc=：每次 post 行为一致（同一响应 / 同一异常）；
    - responses=[...]：第 n 次 post 消费第 n 项，项为 Exception 实例时
      抛出该异常，否则作为响应返回（用于同实例多次搜索返回不同结果）。
    """

    last_instance = None

    def __init__(self, resp=None, exc=None, responses=None):
        self._resp = resp
        self._exc = exc
        self._responses = list(responses) if responses is not None else None
        self.post_calls = []
        _FakeAsyncClient.last_instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        if self._responses is not None:
            item = self._responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if self._exc is not None:
            raise self._exc
        return self._resp


def _patch_httpx(monkeypatch, resp=None, exc=None, responses=None):
    fake = _FakeAsyncClient(resp=resp, exc=exc, responses=responses)
    monkeypatch.setattr(bocha_module.httpx, "AsyncClient", lambda *a, **kw: fake)
    return fake


def _patch_config(monkeypatch, api_key="test-key", api_base="https://api.bocha.cn"):
    monkeypatch.setattr(bocha_module, "BOCHA_API_KEY", api_key)
    monkeypatch.setattr(bocha_module, "BOCHA_API_BASE", api_base)


def _make_success_payload(pages):
    """构造博查 web-search 成功响应体。"""
    return {
        "code": 200,
        "log_id": "abc",
        "msg": None,
        "data": {"webPages": {"value": pages}},
    }


_PAGES = [
    {
        "name": "标题一",
        "url": "https://example.com/1",
        "snippet": "片段一",
        "summary": "摘要一",
        "siteName": "站点一",
        "dateLastCrawled": "2026-01-01",
        "extraField": "不应进入 bocha_sum",
    },
    {
        "name": "标题二",
        "url": "https://example.com/2",
        "snippet": "片段二",
        # summary/siteName/dateLastCrawled 缺失 → 补空串
    },
]


def _chunk_text(chunk) -> str:
    return chunk.content[0].text


# ---------------------------------------------------------------------------
# 工厂与入参/配置校验
# ---------------------------------------------------------------------------


def test_tool_factory_returns_named_tool():
    tool = create_bocha_search_tool()
    assert tool.name == "bocha_web_search"
    assert tool.description


@pytest.mark.asyncio
async def test_empty_query_rejected_without_http_call(monkeypatch):
    """空/空白 query：直接返回提示文本（正常状态），不发起 HTTP 请求。"""
    _patch_config(monkeypatch)
    fake = _patch_httpx(monkeypatch)
    tool = create_bocha_search_tool()

    expected = (
        "搜索关键词为空。请携带明确的搜索关键词（query 参数）重新调用本工具。"
    )
    for q in ("", "   "):
        chunk = await tool(query=q)
        assert _chunk_text(chunk) == expected
        assert chunk.metadata == {}
    assert fake.post_calls == []


@pytest.mark.asyncio
async def test_missing_api_key_returns_unconfigured(monkeypatch):
    """API key 未配置：返回未配置提示，不外呼。"""
    _patch_config(monkeypatch, api_key="")
    fake = _patch_httpx(monkeypatch)
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索服务未配置，请联系管理员。"
    assert chunk.metadata == {}
    assert fake.post_calls == []


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_returns_text_and_bocha_sum_metadata(monkeypatch):
    """成功：请求 url/headers/payload 正确；文本含序号标题链接；
    bocha_sum 字段子集 + citation_id 写入 metadata。"""
    _patch_config(monkeypatch, api_key="sk-test")
    fake = _patch_httpx(
        monkeypatch, resp=_FakeResponse(json_data=_make_success_payload(_PAGES)),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="今日科技新闻")

    # 请求形态：{base}/v1/web-search、Bearer 鉴权、query + summary/count
    assert len(fake.post_calls) == 1
    call = fake.post_calls[0]
    assert call["url"] == "https://api.bocha.cn/v1/web-search"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["json"] == {"query": "今日科技新闻", "summary": True, "count": 10}

    # 返回文本：序号 + 标题 + 摘要/片段 + 链接
    text = _chunk_text(chunk)
    assert "[1] 标题一" in text
    assert "摘要一" in text
    assert "链接: https://example.com/1" in text
    assert "[2] 标题二" in text

    # metadata.bocha_sum：只保留子集字段 + citation_id，缺失补空串
    bocha_sum = chunk.metadata["bocha_sum"]
    assert len(bocha_sum) == 2
    assert bocha_sum[0] == {
        "name": "标题一", "url": "https://example.com/1", "snippet": "片段一",
        "summary": "摘要一", "siteName": "站点一", "dateLastCrawled": "2026-01-01",
        "citation_id": 1,
    }
    assert bocha_sum[1]["summary"] == ""
    assert bocha_sum[1]["siteName"] == ""
    assert bocha_sum[1]["citation_id"] == 2
    assert all("extraField" not in s for s in bocha_sum)


@pytest.mark.asyncio
async def test_api_base_trailing_slash_normalized(monkeypatch):
    """API base 带尾部斜杠时拼接仍正确（rstrip 处理）。"""
    _patch_config(monkeypatch, api_base="https://api.bocha.cn/")
    fake = _patch_httpx(
        monkeypatch, resp=_FakeResponse(json_data=_make_success_payload(_PAGES)),
    )
    tool = create_bocha_search_tool()

    await tool(query="q")
    assert fake.post_calls[0]["url"] == "https://api.bocha.cn/v1/web-search"


# ---------------------------------------------------------------------------
# 失败路径（均不写 metadata）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_friendly_message(monkeypatch):
    _patch_config(monkeypatch)
    _patch_httpx(monkeypatch, exc=httpx.TimeoutException("timed out"))
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索请求超时，请稍后重试。"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_network_error_returns_friendly_message(monkeypatch):
    _patch_config(monkeypatch)
    _patch_httpx(monkeypatch, exc=ConnectionError("dns fail"))
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索网络异常: dns fail"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_http_error_status_returns_friendly_message(monkeypatch):
    _patch_config(monkeypatch)
    _patch_httpx(
        monkeypatch, resp=_FakeResponse(status_code=403, text="forbidden"),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索接口返回错误: HTTP 403"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_invalid_json_returns_friendly_message(monkeypatch):
    _patch_config(monkeypatch)
    _patch_httpx(monkeypatch, resp=_FakeResponse(status_code=200, text="not json"))
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索响应解析失败，请稍后重试。"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_business_error_code_returns_friendly_message(monkeypatch):
    """code != 200 且非 None：业务错误。"""
    _patch_config(monkeypatch)
    _patch_httpx(
        monkeypatch, resp=_FakeResponse(json_data={"code": 40001, "msg": "bad"}),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="新闻")
    assert _chunk_text(chunk) == "搜索服务返回错误，请稍后重试。"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_no_web_pages_returns_no_result(monkeypatch):
    """webPages.value 为空列表或缺失：未搜索到相关结果。"""
    _patch_config(monkeypatch)
    _patch_httpx(
        monkeypatch,
        resp=_FakeResponse(json_data={"code": 200, "data": {"webPages": {"value": []}}}),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="不存在的关键词xyz")
    assert _chunk_text(chunk) == "未搜索到相关结果。"
    assert chunk.metadata == {}


# ---------------------------------------------------------------------------
# 请求级统一编号（citation_id）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_instance_shares_continuous_numbering(monkeypatch):
    """同实例连续两次搜索：编号连续递增且正文仅展示本次条目；
    metadata 为截至当前的完整来源表快照（按编号升序）。"""
    _patch_config(monkeypatch)
    pages_first = [
        {"name": "A", "url": "https://example.com/a", "summary": "摘要A"},
        {"name": "B", "url": "https://example.com/b", "summary": "摘要B"},
    ]
    pages_second = [
        {"name": "B", "url": "https://example.com/b", "summary": "摘要B"},
        {"name": "C", "url": "https://example.com/c", "summary": "摘要C"},
    ]
    _patch_httpx(monkeypatch, responses=[
        _FakeResponse(json_data=_make_success_payload(pages_first)),
        _FakeResponse(json_data=_make_success_payload(pages_second)),
    ])
    tool = create_bocha_search_tool()

    chunk1 = await tool(query="第一次")
    text1 = _chunk_text(chunk1)
    assert "[1] A" in text1
    assert "[2] B" in text1
    m1 = chunk1.metadata["bocha_sum"]
    assert [s["citation_id"] for s in m1] == [1, 2]

    chunk2 = await tool(query="第二次")
    text2 = _chunk_text(chunk2)
    # B 沿用编号 2，C 新登记为 3；正文不重复展示首次的 A
    assert "[2] B" in text2
    assert "[3] C" in text2
    assert "[1] A" not in text2
    # metadata 是完整来源表快照：3 条、按编号升序
    m2 = chunk2.metadata["bocha_sum"]
    assert [s["citation_id"] for s in m2] == [1, 2, 3]
    assert [s["name"] for s in m2] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_duplicate_url_reuses_id_and_first_registration(monkeypatch):
    """重复 URL：沿用首次登记的编号与条目（name/summary 以首次为准），
    快照无新增。"""
    _patch_config(monkeypatch)
    pages_first = [
        {"name": "首次标题", "url": "https://example.com/dup",
         "summary": "首次摘要", "snippet": "首次片段"},
    ]
    pages_second = [
        {"name": "新标题", "url": "https://example.com/dup",
         "summary": "新摘要", "snippet": "新片段"},
    ]
    _patch_httpx(monkeypatch, responses=[
        _FakeResponse(json_data=_make_success_payload(pages_first)),
        _FakeResponse(json_data=_make_success_payload(pages_second)),
    ])
    tool = create_bocha_search_tool()

    chunk1 = await tool(query="第一次")
    assert "[1] 首次标题" in _chunk_text(chunk1)

    chunk2 = await tool(query="第二次")
    text2 = _chunk_text(chunk2)
    # 同一编号 + 首次登记的 name/summary/snippet
    assert "[1] 首次标题" in text2
    assert "首次摘要" in text2
    assert "新标题" not in text2
    assert "新摘要" not in text2
    # 快照保留首次登记信息，无新增条目
    m2 = chunk2.metadata["bocha_sum"]
    assert len(m2) == 1
    assert m2[0] == {
        "name": "首次标题", "url": "https://example.com/dup",
        "snippet": "首次片段", "summary": "首次摘要",
        "siteName": "", "dateLastCrawled": "", "citation_id": 1,
    }


@pytest.mark.asyncio
async def test_blank_or_missing_urls_filtered_partial(monkeypatch):
    """部分有效：空串/空白/缺失 URL 均被过滤，仅有效条目登记编号 1。"""
    _patch_config(monkeypatch)
    pages = [
        {"name": "空URL", "url": "", "summary": "不应出现"},
        {"name": "空白URL", "url": "   ", "summary": "不应出现"},
        {"name": "缺失URL", "summary": "不应出现"},  # extract 后 url 为空串
        {"name": "有效条目", "url": " https://example.com/ok ",
         "summary": "有效摘要"},
    ]
    _patch_httpx(
        monkeypatch, resp=_FakeResponse(json_data=_make_success_payload(pages)),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="q")
    text = _chunk_text(chunk)
    assert "[1] 有效条目" in text
    assert "有效摘要" in text
    assert "[2]" not in text  # 无效条目不占编号、不进正文
    bocha_sum = chunk.metadata["bocha_sum"]
    assert len(bocha_sum) == 1
    assert bocha_sum[0]["citation_id"] == 1
    assert bocha_sum[0]["url"] == "https://example.com/ok"  # strip 后登记
    assert all(s["name"] != "空URL" for s in bocha_sum)


@pytest.mark.asyncio
async def test_all_invalid_urls_returns_no_result(monkeypatch):
    """全部条目 URL 无效：视为未搜索到相关结果，metadata 为空。"""
    _patch_config(monkeypatch)
    pages = [
        {"name": "空URL", "url": ""},
        {"name": "空白URL", "url": "   "},
        {"name": "缺失URL"},
    ]
    _patch_httpx(
        monkeypatch, resp=_FakeResponse(json_data=_make_success_payload(pages)),
    )
    tool = create_bocha_search_tool()

    chunk = await tool(query="q")
    assert _chunk_text(chunk) == "未搜索到相关结果。"
    assert chunk.metadata == {}


@pytest.mark.asyncio
async def test_failure_does_not_consume_citation_ids(monkeypatch):
    """失败路径不占编号：先超时失败，随后成功搜索编号仍从 1 开始。"""
    _patch_config(monkeypatch)
    pages = [{"name": "A", "url": "https://example.com/a", "summary": "摘要A"}]
    _patch_httpx(monkeypatch, responses=[
        httpx.TimeoutException("timed out"),
        _FakeResponse(json_data=_make_success_payload(pages)),
    ])
    tool = create_bocha_search_tool()

    chunk1 = await tool(query="第一次")
    assert _chunk_text(chunk1) == "搜索请求超时，请稍后重试。"
    assert chunk1.metadata == {}

    chunk2 = await tool(query="第二次")
    assert "[1] A" in _chunk_text(chunk2)
    bocha_sum = chunk2.metadata["bocha_sum"]
    assert len(bocha_sum) == 1
    assert bocha_sum[0]["citation_id"] == 1


@pytest.mark.asyncio
async def test_earlier_snapshot_immutable_after_new_registration(monkeypatch):
    """快照不可变：第二次搜索新增来源后，第一次拿到的 metadata 内容不变。"""
    _patch_config(monkeypatch)
    pages1 = [{"name": "A", "url": "https://example.com/a", "summary": "摘要A"}]
    pages2 = [{"name": "B", "url": "https://example.com/b", "summary": "摘要B"}]
    _patch_httpx(monkeypatch, responses=[
        _FakeResponse(json_data=_make_success_payload(pages1)),
        _FakeResponse(json_data=_make_success_payload(pages2)),
    ])
    tool = create_bocha_search_tool()

    chunk1 = await tool(query="第一次")
    m1 = chunk1.metadata["bocha_sum"]
    m1_before = [dict(s) for s in m1]

    chunk2 = await tool(query="第二次")
    m2 = chunk2.metadata["bocha_sum"]

    # 第二次快照包含两个来源；第一次快照 M1 长度与内容均不变
    assert len(m2) == 2
    assert len(m1) == 1
    assert m1 == m1_before
    assert m1[0]["name"] == "A"
    # 快照为逐字典复制：两次 metadata 中的同名条目不是同一对象
    assert m1[0] is not m2[0]


@pytest.mark.asyncio
async def test_new_instance_restarts_numbering_from_one(monkeypatch):
    """新实例重新编号：另建工具实例后，同一 URL 编号回到 1。"""
    _patch_config(monkeypatch)
    pages = [{"name": "A", "url": "https://example.com/a", "summary": "摘要A"}]
    _patch_httpx(
        monkeypatch, resp=_FakeResponse(json_data=_make_success_payload(pages)),
    )
    tool1 = create_bocha_search_tool()
    chunk1 = await tool1(query="第一次")
    assert "[1] A" in _chunk_text(chunk1)

    tool2 = create_bocha_search_tool()
    chunk2 = await tool2(query="第二次")
    assert "[1] A" in _chunk_text(chunk2)
    bocha_sum = chunk2.metadata["bocha_sum"]
    assert len(bocha_sum) == 1
    assert bocha_sum[0]["citation_id"] == 1


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def test_extract_bocha_sum_field_subset():
    """只保留 _BOCHA_SUM_FIELDS 子集，缺失字段补空串，非 dict 项跳过。"""
    pages = [
        {"name": "n", "url": "u", "unexpected": "x"},
        "not-a-dict",
        None,
    ]
    result = _extract_bocha_sum(pages)
    assert len(result) == 1
    assert set(result[0].keys()) == set(_BOCHA_SUM_FIELDS)
    assert result[0]["name"] == "n"
    assert result[0]["snippet"] == ""


def test_format_search_results_prefers_summary_over_snippet():
    """格式化文本 summary 优先，缺失时回退 snippet；文本编号 [n] 直接取自
    citation_id 字段（非连续值 2/5，证明编号不来自数组位置）；尾注含引用
    规则文案。"""
    bocha_sum = [
        {"name": "A", "url": "http://a", "summary": "摘要A", "snippet": "片段A",
         "citation_id": 2},
        {"name": "B", "url": "http://b", "summary": "", "snippet": "片段B",
         "citation_id": 5},
    ]
    text = _format_search_results(bocha_sum)
    assert "[2] A" in text and "摘要A" in text
    assert "[5] B" in text and "片段B" in text
    assert "[1]" not in text  # 编号取自字段而非位置，不出现局部重编号
    assert "链接: http://a" in text and "链接: http://b" in text
    # 尾注引用规则文案
    assert "统一编号" in text
    assert "不要自行重新编号" in text
    assert "不要沿用历史轮次" in text


def test_build_result_with_bocha_sum():
    chunk = _build_result("text", bocha_sum=[{"name": "x"}])
    assert chunk.is_last is True
    assert chunk.metadata == {"bocha_sum": [{"name": "x"}]}


def test_build_result_without_bocha_sum():
    """无 bocha_sum / 空列表 / None：metadata 均为空 dict。"""
    assert _build_result("text").metadata == {}
    assert _build_result("text", bocha_sum=[]).metadata == {}
    assert _build_result("text", bocha_sum=None).metadata == {}


# ---------------------------------------------------------------------------
# merge_bocha_snapshots：按 citation_id 幂等合并
# ---------------------------------------------------------------------------


def _entry(cid, name="x", url="http://x"):
    """构造带 citation_id 的来源条目。"""
    return {"citation_id": cid, "name": name, "url": url}


def test_merge_empty_existing_sorts_incoming_ascending():
    """existing 为空 + incoming 乱序 → 按 citation_id 升序输出。"""
    incoming = [_entry(3, name="C"), _entry(1, name="A"), _entry(2, name="B")]
    merged = merge_bocha_snapshots([], incoming)
    assert [s["citation_id"] for s in merged] == [1, 2, 3]


def test_merge_same_id_keeps_existing_entry():
    """相同 citation_id 幂等：existing 优先，incoming 同 ID 条目丢弃。"""
    existing = [_entry(1, name="首次登记")]
    incoming = [_entry(1, name="后来快照")]
    merged = merge_bocha_snapshots(existing, incoming)
    assert len(merged) == 1
    assert merged[0]["name"] == "首次登记"


def test_merge_interleaved_ids_sorted_without_regression():
    """乱序不回退：existing 含 1、3，incoming 含 1、2 → 合并为 1、2、3。"""
    existing = [_entry(1), _entry(3)]
    incoming = [_entry(1), _entry(2)]
    merged = merge_bocha_snapshots(existing, incoming)
    assert [s["citation_id"] for s in merged] == [1, 2, 3]


def test_merge_missing_citation_id_appended_and_deduped():
    """缺 citation_id 的条目原样附输出尾部；完全相同的 dict 去重。"""
    tail = {"name": "无编号", "url": "http://tail"}
    existing = [_entry(1), tail]
    incoming = [tail, _entry(2), dict(tail)]
    merged = merge_bocha_snapshots(existing, incoming)
    # 带 ID 部分升序在前，无 ID 条目原样附尾且只出现一次
    assert [s["citation_id"] for s in merged[:2]] == [1, 2]
    assert merged[2:] == [tail]
    assert sum(1 for s in merged if "citation_id" not in s) == 1


def test_merge_skips_non_dict_entries():
    """非 dict 条目（str/int/None）直接跳过。"""
    existing = [_entry(1), "not-a-dict", 42]
    incoming = [None, _entry(2), ["still", "a", "list"]]
    merged = merge_bocha_snapshots(existing, incoming)
    assert merged == [_entry(1), _entry(2)]


def test_merge_is_idempotent_on_repeated_apply():
    """合并两次结果幂等：merge(merge(existing, incoming), incoming) 不变。"""
    existing = [_entry(1, name="A"), _entry(3, name="C")]
    incoming = [_entry(2, name="B"), _entry(3, name="C-重复")]
    merged_once = merge_bocha_snapshots(existing, incoming)
    merged_twice = merge_bocha_snapshots(merged_once, incoming)
    assert merged_once == merged_twice
    assert [s["citation_id"] for s in merged_twice] == [1, 2, 3]
