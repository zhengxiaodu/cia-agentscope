# 用制度问答技能替换 RAGFlow 检索技能

## Summary

将现有 `ragflow_retrieval` 技能替换为新的 `policy_qa` 制度问答技能。新技能调用 `POST /api/v1/policy/qa` 接口，**工具签名只暴露 `question` 一个参数**；`knowledge_base_ids` 由宿主侧代码根据 Redis 中缓存的用户权限（`agent_whitelist` 中的智能体名称）自动映射填充，模型与沙箱均无法获知全部知识库 ID，满足"代码不暴露在外"的安全要求。

## Current State Analysis

### 现有 ragflow_retrieval 技能
- [skills/ragflow_retrieval/SKILL.md](file:///workspace/skills/ragflow_retrieval/SKILL.md)：技能说明，`dataset_ids` 作为模型可见参数
- [skills/ragflow_retrieval/ragflow_retrieval.py](file:///workspace/skills/ragflow_retrieval/ragflow_retrieval.py)：工具实现，直接调用 RAGFlow SDK，`question` + `dataset_ids` 均由模型传入
- [config/skill_config.yml](file:///workspace/config/skill_config.yml) 第 8-9 行注册了该技能

### 宿主侧 FunctionTool 模式（参考 mineru）
- [tools/mineru_tools.py](file:///workspace/tools/mineru_tools.py)：工具实现 + 模块级 `mineru_parse_tool = FunctionTool(...)` 单例
- [skills/mineru/SKILL.md](file:///workspace/skills/mineru/SKILL.md)：技能说明（供模型读），注明"主代码位于 `tools/mineru_tools.py`"
- [skills/mineru/tools.py](file:///workspace/skills/mineru/tools.py)：重导出（与沙箱技能结构一致）
- [app/services/orchestrator_service.py:437](file:///workspace/app/services/orchestrator_service.py#L437)：`from tools.mineru_tools import mineru_parse_tool` 注入 `all_tools`

### 用户权限缓存
- [app/services/auth_service.py:49-83](file:///workspace/app/services/auth_service.py#L49-L83)：`save_user_permissions` / `get_user_permissions`，Redis key=`user_permissions:{user_id}`，value=`{"permissions": {...}}`
- [app/dao/user_dao.py:11-13](file:///workspace/app/dao/user_dao.py#L11-L13)：`permissions` 结构为 `{"agent_whitelist": [{"id","name","code"}, ...], "skill_blacklist": [...]}`
- **关键**：`agent_whitelist[].name` 即"金科制度问答"/"信科制度问答"等智能体权限名称

### orchestrator 会话构建路径
- [app/services/orchestrator_service.py:356-364](file:///workspace/app/services/orchestrator_service.py#L356-L364)：`_build_request_components(user_id, redis_client, ...)` 已持有 `user_id` 与 `redis_client`，适合在此创建闭包工具
- [app/services/orchestrator_service.py:446-460](file:///workspace/app/services/orchestrator_service.py#L446-L460)：`all_tools` 组装点，双后端分支均在此处注入工具

## Proposed Changes

### 1. 新建 `tools/policy_qa_tools.py`（核心工具实现）

**职责**：定义权限→kb_id 映射、工厂函数、工具实现。**该文件在宿主进程执行，不注入沙箱，模型不可见。**

**内容**：
- 模块级常量 `_PERMISSION_KB_MAP`：默认映射 `{"金科制度问答": "123", "信科制度问答": "456"}`，可被 `POLICY_QA_KB_MAP` 环境变量（JSON 格式）覆盖
- `_resolve_kb_ids(permissions: dict) -> list[str]`：遍历 `permissions["agent_whitelist"]`，按 `name` 匹配 `_PERMISSION_KB_MAP`，收集匹配的 kb_id（去重）
- `create_policy_qa_tool(user_id: str, redis_client) -> FunctionTool`：工厂函数，返回闭包工具
  - 闭包工具签名：`async def policy_qa(question: str) -> ToolChunk`（**只暴露 question**）
  - 内部流程：
    1. `await get_user_permissions(redis_client, user_id)` 取权限
    2. `_resolve_kb_ids(permissions)` 得到 kb_ids
    3. kb_ids 为空 → 返回 `ToolChunk(text="您没有制度问答权限，请联系管理员开通。")`
    4. kb_ids 非空 → `httpx.AsyncClient` POST `{POLICY_QA_BASE_URL}/api/v1/policy/qa`，body=`{question, knowledge_base_ids: kb_ids}`，超时 120s
    5. 解析响应：把 `answer` + `citations`（position/document_name/page_start-page_end）格式化为文本返回
    6. 异常：返回友好错误提示，不抛出
- `FunctionTool(func=policy_qa, name="policy_qa", description="调用制度问答 API，基于知识库回答制度/政策/法规类问题。")`

**安全性保证**：
- `_PERMISSION_KB_MAP` 在宿主内存，不注入沙箱，模型无法读取
- SKILL.md 不含任何具体 kb_id 或映射关系
- 工具签名只有 `question`，模型无法指定/探测 kb_id
- 即使模型在沙箱内执行 bash，也无法访问宿主的 Redis 或 `_PERMISSION_KB_MAP`

### 2. 新建 `skills/policy_qa/SKILL.md`（技能说明）

**基于用户提供的说明文档，但做以下关键修改**：
- frontmatter `name: policy_qa`，`description` 调用制度问答 API
- **移除 `knowledge_base_ids` 参数**（从请求参数表、请求示例、curl 示例、Python 示例中全部删除）
- 工具入参只保留 `question: string`（必填）
- 新增说明："知识库 ID 列表由系统根据当前用户权限自动填充，调用方无需（也无法）指定。模型只需传入用户问题文本。"
- **不出现** "123"/"456"/"金科制度问答"/"信科制度问答" 等映射关系
- 保留响应格式、错误码、使用要点（适配为单参数版本）
- 保留 `metadata.tools: [policy_qa]`

### 3. 新建 `skills/policy_qa/tools.py`（重导出，与 mineru 模式一致）

```python
from tools.policy_qa_tools import create_policy_qa_tool
```

注：沙箱模式下技能目录会被注入沙箱，但 `create_policy_qa_tool` 工厂在宿主侧调用，沙箱内的 `tools.py` 仅保持结构一致，不会被实际执行（与 mineru 一致）。

### 4. 修改 `app/config.py`（新增配置项）

在 RAGFLOW 配置附近新增：
```python
# ---- 制度问答（policy_qa）配置 ----
POLICY_QA_BASE_URL = os.getenv("POLICY_QA_BASE_URL", "http://25.59.38.160:6181")
# 权限名 → 知识库 ID 映射（JSON 格式），覆盖默认映射
# 示例: POLICY_QA_KB_MAP={"金科制度问答":"123","信科制度问答":"456"}
POLICY_QA_KB_MAP = os.getenv("POLICY_QA_KB_MAP", "")
```

### 5. 修改 `.env` 与 `.env.example`（新增配置段）

在 RAGFLOW 配置段后新增：
```env
# 制度问答配置（policy_qa 技能）
POLICY_QA_BASE_URL=http://25.59.38.160:6181
# 权限名→知识库ID 映射（JSON，留空则用代码默认值）
POLICY_QA_KB_MAP=
```

### 6. 修改 `config/skill_config.yml`（替换技能注册）

将：
```yaml
  - name: ragflow_retrieval
    directory: ../skills/ragflow_retrieval
    description: "使用ragflow知识库检索公司内部制度知识"
```
替换为：
```yaml
  - name: policy_qa
    directory: ../skills/policy_qa
    description: "调用制度问答 API，基于知识库回答制度/政策/法规类问题。"
```

### 7. 修改 `app/services/orchestrator_service.py`（注入工具）

在 `_build_request_components` 的工具组装段（L446-460 附近）：
- 在两个后端分支**之前**创建 policy_qa 工具（因它不依赖后端类型，是宿主侧 FunctionTool）：
  ```python
  from tools.policy_qa_tools import create_policy_qa_tool
  policy_qa_tool = create_policy_qa_tool(user_id=user_id_safe, redis_client=redis_client)
  ```
- 在两个分支的 `all_tools` 列表末尾追加 `policy_qa_tool`：
  - opensandbox 分支：`all_tools = create_opensandbox_tools(adapter) + _chart_tools + [mineru_parse_tool, policy_qa_tool]`
  - docker 分支：`all_tools = [Bash(), Read(), Write(), Edit(), Glob(), Grep()] + _chart_tools + [mineru_parse_tool, policy_qa_tool]`

注：`user_id` 与 `redis_client` 已在 `_build_request_components` 作用域内（L358-359），无需额外传参。

### 8. 删除 `skills/ragflow_retrieval/` 目录

删除 `skills/ragflow_retrieval/SKILL.md` 与 `skills/ragflow_retrieval/ragflow_retrieval.py`。

### 9. 检查 `app/my-workspace/skills/` 同步

检查 `app/my-workspace/skills/` 是否有 ragflow_retrieval 副本需要清理，以及是否需要放入 policy_qa 副本（与 bocha_search/card_interaction 一致）。若该目录无 ragflow_retrieval，则无需处理。

## Assumptions & Decisions

1. **工具签名只暴露 `question`**：用户"两个必填项"指后端接口契约（question + knowledge_base_ids），模型工具签名只暴露 question，kb_ids 由宿主侧自动填充。这同时满足"不暴露全部知识库 ID"。
2. **映射配置策略**：默认映射硬编码在 `tools/policy_qa_tools.py` 的 `_PERMISSION_KB_MAP`，可被 `POLICY_QA_KB_MAP` 环境变量（JSON）覆盖。开发/生产可差异化配置，且映射逻辑不经过沙箱。
3. **无权限行为**：用户无任何制度问答权限时，工具返回友好提示 `"您没有制度问答权限，请联系管理员开通。"`，不抛异常，不调用后端接口。
4. **多权限合并**：用户同时拥有"金科制度问答"和"信科制度问答"权限时，kb_ids 列表包含两个 ID（`["123","456"]`），后端做多库联合召回。
5. **宿主侧执行**：policy_qa 工具是宿主侧 FunctionTool（与 mineru 一致），不依赖工作区后端（docker/opensandbox），两种后端下行为一致。
6. **Base URL 配置化**：`http://25.59.38.160:6181` 不硬编码在 SKILL.md，改为 `POLICY_QA_BASE_URL` 环境变量。
7. **技能替换=删除+新增**：完全删除 ragflow_retrieval 技能目录并从 skill_config.yml 移除，新增 policy_qa。
8. **保留 RAGFLOW 配置**：`app/config.py` 中的 `RAGFLOW_API_KEY`/`RAGFLOW_BASE_URL` 暂不删除（避免破坏其他潜在引用），仅移除技能注册。

## Verification

1. **语法校验**：`python -c "import ast; ast.parse(open('tools/policy_qa_tools.py').read()); ast.parse(open('app/config.py').read()); ast.parse(open('app/services/orchestrator_service.py').read()); print('OK')"`
2. **技能注册校验**：`config/skill_config.yml` 含 `policy_qa`，不含 `ragflow_retrieval`
3. **安全性校验**：
   - `skills/policy_qa/SKILL.md` 不含 "123"/"456"/"金科制度问答"/"信科制度问答" 字样
   - `tools/policy_qa_tools.py` 的 `_PERMISSION_KB_MAP` 不在沙箱注入路径上
   - 工具签名只有 `question` 参数
4. **功能校验**（需运行环境）：
   - 用户有"金科制度问答"权限 → 工具调用后端，kb_ids 含 "123"
   - 用户无权限 → 返回友好提示
   - 用户有两个权限 → kb_ids 含 ["123","456"]
5. **回归校验**：`ragflow_retrieval` 目录已删除，其他技能（bocha_search/chart_renderer/card_interaction/mineru）注册不变
