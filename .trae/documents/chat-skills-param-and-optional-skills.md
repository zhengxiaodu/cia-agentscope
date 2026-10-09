# /chat 新增 skills 请求参数 + optional_skills 返回参数

## 摘要

在 `/chat` 请求体新增 `skills: List[str]`（技能名称列表）。列表中的技能**无论是否绑定智能体**，都绑定到本次请求新建的**每一个** agent 的 Toolkit 中。同时在认证接口返回 `optional_skills: ["mineru","chart_renderer"]`，告知前端哪些技能可选。

## 现状分析

### 技能装载链路（已探索）
1. **内置技能**（`skill_config.yml` 的 5 个：bocha_search/chart_renderer/card_interaction/ragflow_retrieval/mineru）始终全量挂载到 workspace，始终在 `all_skills_meta` 中可用。
2. **agent 绑定**（[registry.py](file:///workspace/app/agents/registry.py) `_build_toolkit_for` L69-90）：按 `definition.skills`（agent_config.yml 中该 agent 绑定的技能名）从 `all_skills_meta` 筛选 loader 组装 Toolkit。

### 透传链（已探索）
`ChatRequest`（[models/chat.py](file:///workspace/app/models/chat.py)）→ [chat.py](file:///workspace/app/routes/chat.py) `generate_response(...)` → [chat_service.py](file:///workspace/app/services/chat_service.py) `orchestrator_service.run(...)` → [orchestrator_service.py](file:///workspace/app/services/orchestrator_service.py) `_build_request_components(...)` → `AgentRegistry(...)`。现有 `search_enabled` 沿此链透传，`skills` 同理。

### AgentRegistry 生命周期
- 每次请求在 `_build_request_components`（L448）新建 `AgentRegistry`，请求结束出作用域释放。
- 单 agent 路径（`_run_single_agent_path`）也经 `agent_factory.create_for_agent → registry.create_agent → get_toolkit → _build_toolkit_for`，故改动 registry 即覆盖所有 agent 创建路径。

### 认证响应（已探索）
[auth.py](file:///workspace/app/routes/auth.py) 三处构造响应：`_build_auth_success`（login+register）/ `_build_update_response`（改资料）/ `refresh_token`，均含 `agent_access` / `skill_blacklist`。

### 关键约束
- skills 仅来自内置技能，内置技能始终装载可用，**无需涉及权限过滤逻辑**。
- 不动 `merged_skills` / `all_skill_dirs` / `all_skills_meta`（workspace 装载策略不变）。

## 设计决策

1. **请求技能在 registry 层 union**：`_build_toolkit_for` 有效绑定 = `definition.skills ∪ extra_skill_names`，按 `all_skills_meta` 取 loader。
2. **透传链仿 `search_enabled`**：参数从 ChatRequest 一路透传到 AgentRegistry 构造。
3. **`optional_skills` 写死常量**：`_OPTIONAL_SKILLS = ["mineru","chart_renderer"]`，注入认证响应。
4. **未装载技能记 warning 跳过**：请求了 workspace 没有的技能名不报错，降级保证可用性。

## 具体改动（6 个文件）

### 一、app/models/chat.py — 新增 skills 字段

```python
class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    search_enabled: bool = True
    skills: List[str] = []  # 新增：请求级附加技能名，绑定到本次每个新建 agent
```

### 二、app/routes/chat.py — 透传 skills

`generate_response(...)` 调用（L38-48）新增 `skills=body.skills`。

### 三、app/services/chat_service.py — generate_response 透传

签名（L364-374）新增 `skills: List[str] = None`；`orchestrator_service.run(...)` 调用（L407-414）新增 `skills=skills or []`。

### 四、app/services/orchestrator_service.py

#### 4.1 `run()` — 签名 + 透传
签名（L647 附近）新增 `skills: List[str] = None`；调用 `_build_request_components`（L685 附近）透传 `skills=skills or []`。

#### 4.2 `_build_request_components` — 接收 skills + 透传 registry
签名（L355-362）新增 `skills: List[str] = None`；构造 `AgentRegistry`（L448-454）新增 `extra_skill_names=skills or []`：

```python
registry = AgentRegistry(
    definitions=agent_defs,
    workspace=workspace,
    all_tools=all_tools,
    all_skills_meta=all_skills_meta,
    create_model_fn=self._create_model_fn,
    extra_skill_names=skills or [],  # 新增
)
```

> 不修改 `merged_skills` / `all_skill_dirs` / `all_skills_meta`。

### 五、app/agents/registry.py — union 请求技能

构造函数（L37-60）新增 `extra_skill_names: Optional[List[str]] = None`；`_build_toolkit_for`（L69-90）union：

```python
def __init__(self, definitions, workspace, all_tools, all_skills_meta, create_model_fn,
             extra_skill_names: Optional[List[str]] = None):
    ...
    self._extra_skill_names = set(extra_skill_names or [])
    ...

def _build_toolkit_for(self, definition: AgentDefinition) -> Toolkit:
    # 有效绑定技能 = agent 自身绑定 ∪ 请求级附加技能
    bound_skill_names = set(definition.skills) | self._extra_skill_names
    if not bound_skill_names:
        return Toolkit(tools=[], skills_or_loaders=[])

    bound_loaders = []
    matched_names = set()
    for meta in self._all_skills_meta:
        meta_name = getattr(meta, "name", None) or (
            meta.get("name") if isinstance(meta, dict) else None
        )
        if meta_name in bound_skill_names:
            bound_loaders.append(meta)
            matched_names.add(meta_name)

    missing = bound_skill_names - matched_names
    if missing:
        logger.warning(
            f"[AgentRegistry] 技能未在 workspace 装载，已跳过: {sorted(missing)}"
        )
    return Toolkit(tools=self._all_tools, skills_or_loaders=bound_loaders)
```

### 六、app/routes/auth.py — optional_skills 响应

模块级常量 + 注入 3 个响应构造处：

```python
# 可选技能（前端据此渲染技能开关）
_OPTIONAL_SKILLS = ["mineru", "chart_renderer"]
```

`_build_auth_success`（L97-107）/ `_build_update_response`（L173-181）/ `refresh_token`（L245-253）的 `success_response({...})` data dict 各新增 `"optional_skills": _OPTIONAL_SKILLS`。

## 不改动的部分

- `mng_service.py`（不涉及权限过滤）
- `skill_config.yml` / `agent_config.yml` / `AgentDefinition` / `AgentFactory`
- `merged_skills` / `all_skill_dirs` / `all_skills_meta`（workspace 装载不变）
- 意图识别 / 编排 / 改写逻辑
- 单 agent 路径（自动受益于 registry 改动）

## 假设与决策

1. **skills 仅来自内置技能**：内置技能始终装载可用，无需权限过滤。若未来支持外部技能，需额外处理（本次不涉及）。
2. **请求技能在 registry 层 union，不动 workspace 装载**：避免 workspace 缓存导致的技能集固化 BUG。
3. **`optional_skills` 写死常量**：用户明确要求。放 auth.py 模块级。
4. **`skills` 默认空列表**：向后兼容，不传时行为不变。
5. **未装载技能记 warning 跳过**：降级保证可用性。

## 验证步骤

1. **AST 校验**：models/chat.py / chat.py / chat_service.py / orchestrator_service.py / registry.py / auth.py
2. **透传链核对**：grep `skills` 确认 ChatRequest → chat.py → generate_response → run → _build_request_components → AgentRegistry 完整
3. **union 逻辑**：agent 自身无该技能 + 请求含该技能 → Toolkit 含该技能 loader；请求技能未装载 → warning 跳过
4. **单 agent 路径**：传 agent_id + skills → 该 agent Toolkit 含请求技能
5. **认证响应**：login/register/refresh/update 响应含 `optional_skills: ["mineru","chart_renderer"]`
6. **向后兼容**：不传 skills 时行为不变
7. **git commit & push** 到 `origin/trae/agent-5CYjia`
