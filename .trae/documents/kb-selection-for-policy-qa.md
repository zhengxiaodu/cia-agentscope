# 制度问答知识库选择 + 可见文档列表功能

## Context

现状：`policy_qa` 工具每次调用时，把用户权限映射出的**全部** kb_ids 传给检索服务做联合召回（`_resolve_kb_ids` 只看权限不看问题），用户无法指定只查某个知识库。

需求：登录时把用户可访问的知识库名列表下发给前端，前端在制度问答智能体下渲染知识库**多选**器；`/chat` 请求携带所选知识库名，后端工具闭包按选择过滤 kb_ids；**空/缺省 = 保持现状全选**。所选库名与实时权限无交集时，返回提示引导重新登录（不做静默回退）。

已确认的决策：
- 选择模式：多选（协议用列表承载）
- 全部失效（交集为空）→ 返回"无权限"提示，引导重新登录刷新权限（Redis 权限为登录时快照，会话中不重拉）
- 部分命中（选了 [有效A, 无效B]）→ 静默忽略无效项，用交集执行；仅交集为空才提示。理由：前端选择器由登录快照渲染，正常流程不会混入无效名；该场景只在权限被重新登录覆盖后的旧会话中出现，宽松交集避免一个失效项阻断整轮提问
- 字段命名全链路统一为 `knowledge_bases`（下发项字段 = 请求体字段，前端取出后原样回传，零转换）

## 改动方案

### 1. tools/policy_qa_tools.py（核心）

- 新增 `_resolve_kb_entries(permissions) -> list[tuple[str, str]]`：返回 `(权限名, kb_id)` 列表，逻辑复用现有 `_resolve_kb_ids` 的遍历+去重（[L54-86](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/tools/policy_qa_tools.py#L54-L86)）
- `_resolve_kb_ids` 改为基于 entries 实现：`[kb_id for _, kb_id in _resolve_kb_entries(permissions)]`（对外语义不变，现有测试不动）
- 新增公开函数 `resolve_user_kb_names(permissions) -> list[str]`：返回 `[name for name, _ in entries]`，供 auth.py 下发前端
- `create_policy_qa_tool(user_id, redis_client, knowledge_bases=None)` 新增第三参数，闭包捕获；工具执行时（在现有"无权限"检查之后、调 `run_general_qa` 之前）：

```python
entries = _resolve_kb_entries(permissions)
if knowledge_bases:
    selected = set(knowledge_bases)
    entries = [(n, k) for n, k in entries if n in selected]
    if not entries:
        return _build_result("您没有所选知识库的访问权限，请退出重新登录后重试")
kb_ids = [k for _, k in entries]
if not kb_ids:
    return _build_result("您没有制度问答权限，请联系管理员开通。")
logger.info(f"[policy_qa] knowledge_bases={knowledge_bases} -> kb_ids={kb_ids}")
```

末行日志补齐可观测性：选择发生在闭包内、不在工具入参里，Langfuse 埋点看不到，需靠日志排查。

### 2. 请求参数透传链（复刻 skills/search_enabled 既有模式）

| 文件 | 改动 |
|---|---|
| [models/chat.py](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/app/models/chat.py) | `ChatRequest` 新增 `knowledge_bases: List[str] = []` |
| [routes/chat.py](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/app/routes/chat.py)（L115-127） | `generate_response(...)` 调用处透传 `knowledge_bases=body.knowledge_bases` |
| [services/chat_service.py](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/app/services/chat_service.py)（L539 `generate_response`） | 签名新增 `knowledge_bases: Optional[List[str]] = None`，透传给 `orchestrator_service.run` |
| [services/orchestrator_service.py](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/app/services/orchestrator_service.py) | `run()`（L564）、`_prepare_workspace_components`（L352）签名新增并透传（L630-641 调用处） |
| [services/workspace_assembler.py](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/app/services/workspace_assembler.py) | `assemble_workspace_components`（L23）签名新增；L94 改为 `create_policy_qa_tool(user_id=user_id, redis_client=redis_client, knowledge_bases=knowledge_bases)` |

`knowledge_bases` 默认 None/空列表 → 工具行为与现状完全一致（全量），不带新字段的前端请求零影响。

### 3. routes/auth.py（登录响应下发知识库名）

- import `resolve_user_kb_names`（from tools.policy_qa_tools；无循环依赖：tools → services/auth_service，不回 routes）
- 新增公共辅助 `_build_agent_access(permissions: dict) -> list`，**收编现有重复代码**：

```python
def _build_agent_access(permissions: dict) -> list:
    """agent_access 统一构造：合并制度问答项 → 清洗 → 注入可选知识库名。"""
    agent_access = _normalize_agent_access(
        _merge_regulations_agents(permissions.get("agent_whitelist", []))
    )
    for item in agent_access:
        if isinstance(item, dict) and item.get("id") == "regulations_agent":
            item["knowledge_bases"] = resolve_user_kb_names(permissions)
            break
    return agent_access
```

- 两处响应构造替换为调用它（同时消除存量重复）：
  - `_build_auth_success`（L229-231，登录/注册共用）
  - `_build_update_response`（L306-308，refresh/改名/改部门/改密共用）
- 无 `regulations_agent` 合并项时不注入字段（行为不变，非制度问答用户零感知）

### 前端契约（本次不改前端，仅约定）

- 登录/刷新/改资料响应：`agent_access` 中 `regulations_agent` 项新增 `"knowledge_bases": ["金科制度问答", "信科制度问答"]`（用户权限内可访问的库名，即 POLICY_QA_KB_MAP 匹配键；kb_id 不出后端）
- `/chat` 请求体新增 `"knowledge_bases": ["金科制度问答", ...]`；空数组或字段缺省 = 全选（现状）
- 前端应在本会话期间持续携带用户的选择（直到用户显式改回全选），避免轮次间选择漂移

## 验证

1. 单测（扩展 [tests/test_regulations_tool.py](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/tests/test_regulations_tool.py)，沿用现有 monkeypatch 模式）：
   - `knowledge_bases` 传入部分权限库名 → `run_general_qa` 只收到选中的 kb_ids
   - `knowledge_bases` 混入无效名 + 一个有效名 → 静默忽略无效项，只用交集执行
   - `knowledge_bases` 全部不在权限内 → 返回无权限提示文案，不调 `run_general_qa`
   - `knowledge_bases` 为空/None → 全量 kb_ids（与现状一致）
   - `resolve_user_kb_names` 返回去重后的名字列表
2. 单测（扩展 [tests/test_agent_access_description.py](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/tests/test_agent_access_description.py) 或同文件模式）：
   - `_build_agent_access`：whitelist 含制度问答项 → `regulations_agent` 项带 `knowledge_bases`
   - whitelist 不含制度问答项 → 响应无该合并项、无 `knowledge_bases` 字段
3. 回归：现有 `test_resolve_kb_ids_*`（`_resolve_kb_ids` 语义不变）与 test_agent_access_description.py 全部保持通过
4. 运行：`python -m pytest tests/test_regulations_tool.py tests/test_agent_access_description.py -v`
5. 手动链路（AUTH_MOCK=true 注意：mock whitelist 的 name 为"制度问答"，与默认 KB_MAP key 不匹配，需设 `POLICY_QA_KB_MAP={"制度问答":"123"}` 才能在 mock 下走通）：
   - 登录 → 响应 `agent_access` 中 `regulations_agent` 项带 `knowledge_bases`
   - `/chat` 带 `agent_id=regulations_agent` + `knowledge_bases=["制度问答"]` → 观察日志 `[policy_qa] knowledge_bases=[...] -> kb_ids=[...]` 确认只含选中库

## 不改的东西

- Redis 权限结构、`_merge_regulations_agents` 合并逻辑、`_normalize_agent_access` 清洗逻辑均不动（`_build_agent_access` 只是把既有两处重复调用收编后追加注入）
- 意图编排路径（不带 agent_id）无需单独处理：policy_qa_tool 是全局工具（workspace_assembler L127-129），闭包过滤对两条路径统一生效
- POLICY_QA_KB_MAP 配置方式不动

---

# 第二部分：用户可见知识库文档接口（前端两个新接口）

依据 `policy-qa-visible-documents.md`（kb_app 侧契约）新增两个前端接口，均加在 [routes/policy_qa.py](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/app/routes/policy_qa.py)（与既有 `POST /api/v1/policy/qa` 同文件同风格）。

已确认决策：
- 列文档接口 kb_ids 由**后端权限解析**（Redis 权限 + POLICY_QA_KB_MAP），前端不传 kb_ids，可传 `knowledge_bases`（与 /chat 选择器联动过滤）
- 源文档接口**严格校验 doc_id 归属**：转发前遍历用户权限内文档确认 doc_id 存在，未命中返回 404

## 改动方案

### 1. kb_client.py 扩展（[app/regulations/services/kb_client.py](f:/系统默认/桌面/中债/综合智能应用门户/cia-agentscope/app/regulations/services/kb_client.py)）

复用既有 `REGULATIONS_KB_API_BASE_URL` / `REGULATIONS_KB_INTERNAL_TOKEN` 与 httpx client，新增两个方法：

- `query_documents(kb_ids, page=1, page_size=20, search="") -> dict`：`POST /api/v1/internal/documents/query`，返回响应 `data`（items/total/page/page_size）；复用既有"内部请求"请求头模式（Bearer token + X-Request-ID）
- `get_document_file(doc_id) -> httpx.Response`：`GET /api/v1/internal/documents/{doc_id}/file`，全量读回（制度文档 MB 级，非流式代理，简单可靠）

### 2. policy_qa_tools.py 公共化（与第一部分 §1 合并实现）

把"权限解析 + knowledge_bases 过滤"提为公共函数，问答工具与两个新路由共用：

```python
def resolve_kb_ids(permissions, knowledge_bases=None) -> list[str]:
    """全量解析；knowledge_bases 非空时取交集（可能为空）。"""
    entries = _resolve_kb_entries(permissions)
    if knowledge_bases:
        selected = {str(n) for n in knowledge_bases}
        entries = [(n, k) for n, k in entries if n in selected]
    return [k for _, k in entries]
```

- 问答工具闭包：改调 `resolve_kb_ids(permissions, knowledge_bases)`，交集为空且用户有选择 → "请退出重新登录"提示；无选择且为空 → 现有"请联系管理员开通"文案
- 列文档路由：同一函数，交集为空 → 直接返回空列表（total=0），不调 kb_app

### 3. 路由一：列文档 `POST /api/v1/policy/documents/query`

- 鉴权：本系统 JWT（`current_user`）
- 请求体（Pydantic）：`page: int = 1`、`page_size: int = 20`（1–100）、`search: str = ""`、`knowledge_bases: List[str] = []`（可选联动过滤）
- 流程：Redis 取权限 → `resolve_kb_ids(permissions, knowledge_bases)` → kb_ids 为空直接返回 `{items: [], total: 0, ...}` → `app.state.regulations_kb_client.query_documents(...)` → 透传 data
- 契约：响应字段与 kb_app §2.3 完全一致（doc_id/kb_id/kb_name/filename/file_type/file_size/page_count/summary/example_questions/updated_at），统一信封 `{code: 200, msg, data}`
- 错误：KBClientError → 502；kb_app 业务失败（code≠0）→ 502 透传 message

### 4. 路由二：源文档 `GET /api/v1/policy/documents/{doc_id}/file`

- 鉴权：本系统 JWT
- 严格校验流程：
  1. Redis 取权限 → `resolve_kb_ids(permissions)`（全量，不看选择——查看源文档不受当前选择过滤，列表里展示过的都允许看）
  2. 分页遍历 `query_documents(kb_ids, page, page_size=100)` 直到命中 `doc_id`；遍历完未命中 → 404（文案"文档不存在或您无权查看"）
  3. `get_document_file(doc_id)` → kb_app 404 → 透传 404
- 响应：裸文件流（非信封），透传 `Content-Type`（按 kb_app 映射）、`Content-Disposition`（inline 文件名）、`X-Page-Count` 三个响应头
- 校验遍历无缓存（文档量级百级，内网一两次内部分页调用可接受；后续可加 doc_id→kb_id 进程内 TTL 缓存）

## 验证（第二部分）

1. 单测（新增 tests/test_policy_documents_routes.py 或并入现有测试文件）：
   - 列文档：权限解析 kb_ids 正确传递给 kb_client；knowledge_bases 过滤生效；kb_ids 为空返回空列表不调 kb_app；kb_app 异常 → 502
   - 源文档：doc_id 在权限内 → 转发并透传三个响应头；doc_id 不在权限内 → 404 不调文件接口
   - 运行：`python -m pytest tests/ -v -k "policy"`
2. 手动：AUTH_MOCK 登录（设 `POLICY_QA_KB_MAP={"制度问答":"123"}`、`REGULATIONS_KB_API_BASE_URL` 指向 kb_app）→ 列文档返回 items → 用返回的 doc_id 调源文档接口拿到文件流

