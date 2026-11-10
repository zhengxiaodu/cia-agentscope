# 问答报错友好兜底 + 博查搜索工具化改造（bocha_sum 来源事件流）

## Summary

两个独立改动：

**改动 1 — error 事件友好兜底**：目前编排链路出错时（agent 执行异常、环境准备失败等），后端直接把 `{"type":"error","message":"执行出错: xxx"}` 透传给前端，前端原样显示技术性错误信息。本次在 [chat_service.py](file:///workspace/app/services/chat_service.py) 的编排事件循环中统一拦截 `error` 事件，将 message 替换为固定友好文案 **"抱歉，暂时无法生成回复。请重试并联系管理员"**（原始错误已在各产生点 `logger.exception` 记录，拦截处补一条 warning 含原文案便于排障）；报错轮无任何输出时，以该文案作为 assistant 消息落库（success=False），保证会话历史详情中该轮不空白。

**改动 2 — 博查搜索工具化改造（对标 policy_qa 模式）**：目前博查联网搜索通过 [skills/bocha_search/SKILL.md](file:///workspace/skills/bocha_search/SKILL.md) 引导模型在沙箱内 curl 调用博查 API（API key 硬编码在 SKILL.md 中暴露给沙箱），搜索来源无法进入事件流。本次改造为宿主侧 FunctionTool：新建 `tools/bocha_search_tools.py` 调用博查 Web Search API（`POST {BOCHA_API_BASE}/v1/web-search`），来源摘要以 **`bocha_sum`** 为 key 写入 `ToolChunk.metadata`，复用 citations 的旁路提取机制（AgentEventTracer → `bocha_sum` SSE 事件 → 前端展示 → `messages.bocha_sum` 落库 → 会话历史详情接口返回）。`.env` 新增 `BOCHA_API_BASE` + `BOCHA_API_KEY` 两项配置，其余请求参数用博查默认值。

## Current State Analysis

**error 事件链路（改动 1 现状）**：
- error 事件产生点（全部在 orchestrator_service.py）：agent_id 不存在（L694）、单 agent 执行异常"执行出错: {e}"（L776-779）、无用户输入（L825）、配置装配失败"环境准备失败: {e}"（L842-845）、工作区准备失败（L960/L1064）
- [chat_service.py:584-629](file:///workspace/app/services/chat_service.py#L584-L629)：事件循环**先 `yield event_str` 透传、后解析**——error 事件的 message 原样到达前端
- 落库现状：error 事件不进 `final_output_parts` 也不进 `final_fallback_parts`（仅 `pipeline_intercept`/`react_final` 进 fallback），报错轮 `final_output` 为空 → **不写 assistant 消息**（[chat_service.py:237](file:///workspace/app/services/chat_service.py#L237) `if final_output:` 才追加），会话历史中该轮只有 user 消息

**citations 标杆链路（改动 2 对标）**：
- 工具层：[tools/policy_qa_tools.py:112-123](file:///workspace/tools/policy_qa_tools.py#L112-L123) `_build_result()` 把 `citations` 写入 `ToolChunk.metadata`，agentscope 自动透传到 `ToolResultEndEvent.metadata`
- 提取层：[agent_event_tracer.py:60-87](file:///workspace/app/utils/agent_event_tracer.py#L60-L87) `_collect_citations()` 从 `ToolResultEndEvent.metadata` 提取 `citations` 累积（不依赖 langfuse 启用），`consume_citations()` 返回并清空
- 事件层：编排器路径 [base.py:165-175](file:///workspace/app/orchestrator/base.py#L165-L175) agent 执行结束后 emit `{"type":"policy_qa_citations","citations":[...]}`；单 agent 路径 [orchestrator_service.py:747-760](file:///workspace/app/services/orchestrator_service.py#L747-L760) 同样处理
- 收集层：[chat_service.py:615-616](file:///workspace/app/services/chat_service.py#L615-L616) 解析事件累积 `collected_citations`，传给 `_persist_conversation_history(citations=...)`
- 落库层：[chat_service.py:252-260](file:///workspace/app/services/chat_service.py#L252-L260) assistant 消息 dict 带 `citations` → [mysql_session_dao.py:252-269](file:///workspace/app/dao/mysql_session_dao.py#L252-L269) INSERT 写 `messages.citations`（JSON 列）
- 查询层：[mysql_session_dao.py:141-170](file:///workspace/app/dao/mysql_session_dao.py#L141-L170) `load_messages` SELECT 含 citations 并反序列化 → [session_service.py:96-135](file:///workspace/app/services/session_service.py#L96-L135) `get_session_detail` 组装 `SessionMessage`（[models/session.py:6-15](file:///workspace/app/models/session.py#L6-L15) 含 `citations: List[Any] = []`）返回前端

**博查技能现状**：
- [skills/bocha_search/SKILL.md](file:///workspace/skills/bocha_search/SKILL.md)：引导模型在沙箱内 `curl -X POST https://api.bocha.cn/v1/web-search -H "Authorization: Bearer {硬编码 key}"`（**key 硬编码暴露**，已从仓库移除）
- 绑定：[agent_config.yml:20-23](file:///workspace/config/agent_config.yml#L20-L23) general_agent 绑定 `bocha_search` 技能；[skill_config.yml:2-3](file:///workspace/config/skill_config.yml#L2-L3) 声明技能目录
- 开关：[orchestrator_service.py:63-64](file:///workspace/app/services/orchestrator_service.py#L63-L64) `_SEARCH_SKILL_NAME = "bocha_search"`；`search_enabled=False` 时过滤 `all_skills_meta` 中的技能 + `extra_skills` 不追加（L544-561）
- 工具注册：[orchestrator_service.py:496-537](file:///workspace/app/services/orchestrator_service.py#L496-L537) `all_tools = create_opensandbox_tools(adapter) + _chart_tools + [policy_qa_tool] + md_tools`——**注意 `Toolkit(tools=self._all_tools, ...)`（[registry.py:104](file:///workspace/app/agents/registry.py#L104)）中 tools 对所有 agent 全局可见**，博查工具需按 `search_enabled` 控制注入
- 博查 Web Search API（官方文档）：`POST /v1/web-search`，请求头 `Authorization: Bearer {key}`，请求体 `{"query": "..."}`（其余参数默认）；响应 `data.webPages.value[]`，每条含 `name/url/snippet/summary/siteName/datePublished` 等字段（兼容 Bing 格式）

**数据库迁移机制**：[init_mysql.py:128-140](file:///workspace/app/dao/init_mysql.py#L128-L140) 用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 幂等加列，新字段照此追加。

## Proposed Changes

### 改动 1：error 事件友好兜底

**`app/services/chat_service.py`**（唯一改动文件）：

1. 模块级常量：
```python
# 编排链路 error 事件的统一前端兜底文案（原始错误信息只进日志）
_FRIENDLY_ERROR_MESSAGE = "抱歉，暂时无法生成回复。请重试并联系管理员"
```

2. 事件循环重构（L584-629 区域）：把"解析"提前到"透传"之前，`error` 事件替换文案后再 yield：
```python
async for event_str in orchestrator_service.run(...):
    if _cancelled(): ...

    # 解析提前：error 事件需替换为友好文案后再透传
    payload = None
    if event_str.startswith("data: ") and event_str.endswith("\n\n"):
        try:
            payload = json.loads(event_str[6:].strip())
        except Exception:
            payload = None
    event_type = payload.get("type", "") if payload else ""

    if event_type == "error":
        error_occurred = True
        logger.warning(
            "[chat_service] 编排错误（前端已兜底）: %s",
            payload.get("message", ""),
        )
        yield f"data: {json.dumps({'type': 'error', 'message': _FRIENDLY_ERROR_MESSAGE}, ensure_ascii=False)}\n\n"
    else:
        yield event_str

    # 既有的 ttft / summary / citations / fallback 收集逻辑复用已解析的 payload
    ...
```
（`error_occurred = False` 在循环前初始化；原 try 块内的解析分支保持不变，仅改为使用已解析的 `payload`/`event_type`，解析失败降级逻辑不变）

3. 落库兜底（L631-634 区域，fallback 链末尾追加一级）：
```python
final_output = "\n".join(final_output_parts).strip()
if not final_output:
    final_output = "\n".join(p for p in final_fallback_parts if p).strip()
# 报错轮兜底：无任何输出时用友好文案落库，保证会话历史该轮不空白（success=False 由编排层派生）
if not final_output and error_occurred:
    final_output = _FRIENDLY_ERROR_MESSAGE
```
优先级：`summary` > `pipeline_intercept/react_final fallback` > `error 友好文案`。后续敏感检测/推荐问题/持久化流程不变（文案固定无敏感词，正常走通）。

### 改动 2：博查搜索工具化

**1. `app/config.py` + `.env` + `.env.example` — 新增配置**

config.py 追加（对齐 MINERU 配置段风格）：
```python
# 博查（Bocha）网络搜索配置：Web Search API 基础地址与 API key（留空则工具返回未配置提示）
BOCHA_API_BASE = os.getenv("BOCHA_API_BASE", "https://api.bocha.cn")
BOCHA_API_KEY = os.getenv("BOCHA_API_KEY", "")
```
.env / .env.example 追加：
```
# 博查网络搜索配置（bocha_web_search 工具；地址留空用官方默认）
BOCHA_API_BASE=https://api.bocha.cn
BOCHA_API_KEY=
```

**2. `tools/bocha_search_tools.py`（新建，对标 policy_qa_tools.py）**

- `create_bocha_search_tool() -> FunctionTool`，工具名 `bocha_web_search`，签名只暴露 `query: str`
- 实现：`aiohttp.ClientSession`（项目既有依赖）`POST {BOCHA_API_BASE.rstrip('/')}/v1/web-search`，headers `{"Content-Type": "application/json", "Authorization": f"Bearer {BOCHA_API_KEY}"}`，json 体 `{"query": query.strip()}`（其余参数用博查默认），超时 15s
- 响应解析：`data.webPages.value[]` → 提取子集字段构造 `bocha_sum` 列表，每条：
```python
{"name": ..., "url": ..., "snippet": ..., "summary": ..., "siteName": ..., "datePublished": ...}
```
- 返回 `ToolChunk`：`content` 为格式化搜索结果文本（`[N] 标题\n摘要\n链接: url`，供模型归纳引用），`metadata={"bocha_sum": [...]}`（由框架透传到 `ToolResultEndEvent.metadata`）
- 错误处理全部内捕获返回友好文案（对标 policy_qa）：query 为空 / 未配置 BOCHA_API_KEY / HTTP 非 200 / 超时 / 网络异常 / 空结果各有固定文案，不向 agent 抛异常
- 文档字符串说明安全设计：API key 只在宿主进程环境变量中，不注入沙箱

**3. `app/utils/agent_event_tracer.py` — 提取 bocha_sum**

- `__init__` 新增 `self._collected_bocha_sum: list = []`
- `_collect_citations()` 扩展（方法可更名 `_collect_tool_metadata`，调用处同步）：在同一 `ToolResultEndEvent.metadata` dict 中额外提取 `bocha_sum` key（`isinstance(list) and 非空` 才累积），与 citations 互不干扰
- 新增 `consume_bocha_sum() -> list`（对称 `consume_citations()`：返回并清空）

**4. `app/orchestrator/base.py` + `app/services/orchestrator_service.py` — emit 事件 + 工具注册**

- base.py `_run_single_agent` 末尾（L165-175 citations emit 之后）追加：
```python
bocha_sum = tracer.consume_bocha_sum()
if bocha_sum:
    yield ("data: " + json.dumps(
        {"type": "bocha_sum", "bocha_sum": bocha_sum}, ensure_ascii=False) + "\n\n")
```
- orchestrator_service.py `_run_single_agent_path`（L747-760 citations emit 之后）同样追加
- orchestrator_service.py `_prepare_workspace_components` 工具组装处（L533-537）：
```python
from tools.bocha_search_tools import create_bocha_search_tool
all_tools = (create_opensandbox_tools(adapter) + _chart_tools + [policy_qa_tool] + md_tools)
# 联网搜索工具受请求开关控制：关闭时不注入（技能过滤为既有逻辑，工具同步收口）
if search_enabled:
    all_tools.append(create_bocha_search_tool())
```
（既有 `all_skills_meta` 过滤与 `extra_skills` 追加逻辑不变）

**5. `app/services/chat_service.py` — 收集 + 落库**

- `collected_citations` 旁新增 `collected_bocha_sum: List[dict] = []`
- 事件解析处新增：
```python
if event_type == "bocha_sum":
    collected_bocha_sum.extend(payload.get("bocha_sum") or [])
```
- `_persist_conversation_history` 新增参数 `bocha_sum: Optional[List[dict]] = None`，assistant 消息 dict 追加 `"bocha_sum": bocha_sum if bocha_sum else None`
- 两处调用（正常路径 L662-670、中断 finally 路径 L704-709）都传入 `bocha_sum=collected_bocha_sum`

**6. `app/dao/init_mysql.py` + `app/dao/mysql_session_dao.py` — bocha_sum 列**

- init_mysql.py：`CREATE TABLE messages` 加 `bocha_sum JSON NULL`（citations 行之后）；迁移段加 `ALTER TABLE messages ADD COLUMN IF NOT EXISTS (bocha_sum JSON NULL);`
- mysql_session_dao.py `append_messages`：INSERT 列与参数追加 `bocha_sum`（`json.dumps` 非空 list，否则 None，与 citations 同逻辑）
- mysql_session_dao.py `load_messages`：SELECT 追加 `bocha_sum`，行映射追加 `"bocha_sum": _parse_json_list(r.get("bocha_sum"))`

**7. `app/models/session.py` — SessionMessage**

`SessionMessage` 追加 `bocha_sum: List[Any] = []`（citations 之后）。

**8. `skills/bocha_search/SKILL.md` — 重写为工具说明**

对标 [skills/policy_qa/SKILL.md](file:///workspace/skills/policy_qa/SKILL.md) 结构：front-matter `metadata.tools: [bocha_web_search]`；正文为"何时调用 / 工具信息 / 入参（query）/ 返回格式（格式化搜索结果，编号与 bocha_sum 来源对应）/ 错误处理 / 使用要点"。**删除全部 curl 示例与硬编码 API key**——技能从"教模型 curl"变为"教模型调工具"。

`skill_config.yml`、`agent_config.yml` 不改（bocha_search 技能名与绑定关系保持，general_agent 绑定不变）。

### 测试

**`tests/test_error_fallback.py`（新建）**：
- 编排流含 error 事件：透传给前端的事件 message 为友好文案（非原始文案），且 warning 日志含原文案
- error 轮无 summary/fallback：落库 assistant 消息 content 为友好文案
- error 轮已有 summary（理论上不会发生但防御）：summary 优先，不覆盖
- 无 error 事件：行为与现状完全一致（回归）

**`tests/test_bocha_search_tools.py`（新建，mock aiohttp 或 monkeypatch ClientSession）**：
- 正常返回：ToolChunk.content 为格式化文本、metadata 含 bocha_sum（6 字段齐全）
- query 为空 / BOCHA_API_KEY 为空 / HTTP 500 / 超时 / 网络异常 / 空结果：各自固定文案，metadata 无 bocha_sum，不抛异常

**`tests/test_agent_event_tracer.py`（扩展）**：
- ToolResultEndEvent.metadata 含 `bocha_sum`：`consume_bocha_sum()` 返回；与 `citations` 共存时互不干扰；consume 后清空

**`tests/test_message_pair_and_citations.py`（扩展）**：
- append_messages 带 bocha_sum：INSERT SQL 含新列
- load_messages：bocha_sum None/JSON 字符串 → [] / list
- generate_response 集成：编排流 `bocha_sum` 事件累积落库

**回归**：`python -m pytest tests/ -q` 全量通过。

## Assumptions & Decisions

- **兜底位置单一收口在 chat_service**：所有编排 error 事件必经此处，不在各产生点分散改文案；原始错误信息保留在日志（各产生点已有 logger.exception，拦截处补 warning 记原文案），前端只见友好文案
- **报错轮也落库 assistant 消息**：用户需求核心是前端展示，但不落库会导致会话历史该轮空白（刷新后丢失错误上下文），故以友好文案落库（success=False）；文案优先级 summary > 编排 fallback > 友好文案，避免覆盖已有有效输出
- **error 事件结构不变**：仍为 `{"type":"error","message":...}`，仅 message 值替换，前端零改动；`message_replace`（敏感拦截）、`user_abort`（中断）等非 error 事件不受影响
- **bocha_sum 落库独立字段而非复用 citations**：两者结构不同（citations 为制度文档引用 position/document_name/page；bocha_sum 为网页来源 name/url/snippet/summary/siteName/datePublished），混存会破坏前端渲染契约；用户新增字段诉求明确（"不能叫citations要叫bocha_sum"）
- **SSE 事件命名为 `{"type":"bocha_sum","bocha_sum":[...]}`**：对标 `policy_qa_citations` 事件形态，字段名按用户要求
- **博查技能保留 SKILL.md 但重写内容**（对标 policy_qa 模式）：技能绑定关系（general_agent + extra_skills union + search_enabled 过滤）全部复用不动，仅技能内容从"curl 指引"变为"工具调用指引"；顺带消除 API key 硬编码暴露给沙箱的安全问题
- **工具受 search_enabled 控制**：`Toolkit(tools=all_tools)` 对所有 agent 全局可见，若不控制注入，关闭联网开关后模型仍可直接调用工具绕过；技能过滤（既有）+ 工具注入（新增）双收口
- **请求体只传 query**：用户明确"其他的配置用默认的"（博查默认 summary=true、count 等默认值）；超时 15s 为工具内部常量，不进配置
- **bocha_sum 字段集**：博查 WebPageValue 的 `name/url/snippet/summary/siteName/datePublished` 六字段，覆盖前端来源展示（标题/链接/摘要/站点/日期）所需
- **aiohttp 而非 httpx**：aiohttp 为项目原生依赖（requirements.txt 已有），sensitive_service 同用 aiohttp 风格
- **单 agent 路径与编排器路径都要 emit**：两条执行路径（`_run_single_agent_path` / base.py `_run_single_agent`）各有一份 citations emit 代码，bocha_sum 对称追加，保持双路径行为一致

## Verification

1. `python -m py_compile` 所有改动文件
2. `python -m pytest tests/test_error_fallback.py tests/test_bocha_search_tools.py tests/test_agent_event_tracer.py tests/test_message_pair_and_citations.py -q` 新增/扩展测试通过
3. 全量回归 `python -m pytest tests/ -q` 通过
4. 人工核对：
   - error 事件前端文案为"抱歉，暂时无法生成回复。请重试并联系管理员"，日志含原始错误
   - `.env.example` 含 BOCHA_API_BASE/BOCHA_API_KEY；SKILL.md 无 API key 残留
   - `bocha_sum` 事件 → messages.bocha_sum 落库 → 会话历史详情接口返回三段链路字段名一致
