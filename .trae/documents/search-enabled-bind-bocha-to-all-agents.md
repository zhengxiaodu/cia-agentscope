# search_enabled=True 时给每个智能体绑定 bocha_search

## 摘要

当 `/chat` 请求 `search_enabled=True` 时，将 `bocha_search` 联网搜索技能绑定到本次请求新建的**每一个** agent（无论该 agent 是否在 agent_config.yml 中绑定了 bocha_search）。`search_enabled=False` 时行为不变（所有 agent 均无 bocha_search）。

## 现状分析（已探索确认）

### search_enabled 透传与过滤链
- `ChatRequest.search_enabled`（[models/chat.py:9](file:///workspace/app/models/chat.py#L9)，默认 True）→ [chat.py:47](file:///workspace/app/routes/chat.py#L47) → [chat_service.py:373,415](file:///workspace/app/services/chat_service.py#L373) → [orchestrator_service.py:650,689](file:///workspace/app/services/orchestrator_service.py#L650) → `_build_request_components`（[L360](file:///workspace/app/services/orchestrator_service.py#L360)）。
- 过滤逻辑（[orchestrator_service.py:439-446](file:///workspace/app/services/orchestrator_service.py#L439-L446)）：
  ```python
  if not search_enabled:
      all_skills_meta = [m for m in all_skills_meta if ... != _SEARCH_SKILL_NAME]
  ```
  - `True`：`all_skills_meta` 保留 bocha_search
  - `False`：`all_skills_meta` 移除 bocha_search
- 常量 `_SEARCH_SKILL_NAME = "bocha_search"`（[L60](file:///workspace/app/services/orchestrator_service.py#L60)）。

### agent 技能绑定现状（[agent_config.yml](file:///workspace/config/agent_config.yml)）
| agent_id | skills |
|---|---|
| chart_agent | chart_renderer, card_interaction |
| general_agent | bocha_search, mineru |
| regulations_qa | ragflow_retrieval |

- **当前问题**：`search_enabled=True` 时只有 `general_agent` 能用 bocha_search，`chart_agent`/`regulations_qa` 用不了。

### 已有 union 机制（[registry.py:74-102](file:///workspace/app/agents/registry.py#L74)）
- `AgentRegistry` 构造接收 `extra_skill_names`，`_build_toolkit_for` 中 `bound_skill_names = set(definition.skills) | self._extra_skill_names`，即 extra 技能会 union 到**每个** agent。
- orchestrator 现已透传 `extra_skill_names=skills or []`（[L455](file:///workspace/app/services/orchestrator_service.py#L455)）。

## 设计决策

**复用现有 `extra_skill_names` union 机制**：`search_enabled=True` 时把 `bocha_search` 追加到 extra_skill_names，与请求 skills 参数叠加后传给 AgentRegistry。

- `search_enabled=True`：extra 含 bocha_search → 每个 agent 的 Toolkit 都含 bocha_search loader
- `search_enabled=False`：`all_skills_meta` 已移除 bocha_search → 即使 extra 含它，`_build_toolkit_for` 在 `all_skills_meta` 中匹配不到 → warning 跳过 → 所有 agent 仍无 bocha_search（行为不变）

> 关键：extra 只是"声明绑定意图"，实际能否装入取决于 `all_skills_meta` 是否含该 loader。`search_enabled=False` 时 meta 已移除，故 extra 中的 bocha_search 自然失效，无需额外判断。

## 具体改动（1 个文件）

### app/services/orchestrator_service.py — `_build_request_components`

在构造 `AgentRegistry` 处（[L448-456](file:///workspace/app/services/orchestrator_service.py#L448-L456)），将 `extra_skill_names` 由仅含请求 skills 改为按 search_enabled 叠加 bocha_search：

```python
# ---- 6. 构建临时注册表 ----
agent_defs = [AgentDefinition(**a) for a in merged_agents]

# 请求级附加技能：用户请求 skills ∪（search_enabled 时追加 bocha_search）
extra_skills = list(skills or [])
if search_enabled:
    extra_skills.append(_SEARCH_SKILL_NAME)

registry = AgentRegistry(
    definitions=agent_defs,
    workspace=workspace,
    all_tools=all_tools,
    all_skills_meta=all_skills_meta,
    create_model_fn=self._create_model_fn,
    extra_skill_names=extra_skills,
)
```

> 原 `extra_skill_names=skills or []` 改为 `extra_skill_names=extra_skills`。

## 不改动的部分

- `search_enabled=False` 分支（[L440-446](file:///workspace/app/services/orchestrator_service.py#L440)）逻辑不变
- `models/chat.py` / `chat.py` / `chat_service.py` / `run()` 透传链不变
- `registry.py`（已有 union 机制，无需改）
- `agent_config.yml`（不改静态绑定，运行时动态追加）
- `skill_config.yml` / workspace 装载策略

## 假设与决策

1. **复用 extra_skill_names**：避免新增并行机制，与上一轮 skills 参数改动一致。
2. **False 时无需额外屏蔽**：`all_skills_meta` 已移除 bocha_search，extra 中的 bocha_search 匹配不到自动跳过（记 warning）。该 warning 在 False 场景是预期的，可接受——若希望避免该 warning，可在 False 时从 extra 中剔除 bocha_search，但属可选优化，本方案保持最简。
3. **不重复添加**：`general_agent` 自身已绑 bocha_search，set union 自动去重，无副作用。
4. **向后兼容**：不传 search_enabled 时默认 True，行为与现状一致（每个 agent 都有 bocha_search，general_agent 不受影响）。

## 验证步骤

1. **AST 校验**：orchestrator_service.py
2. **True 场景**：`search_enabled=True` → extra 含 bocha_search → chart_agent / regulations_qa / general_agent 三个 agent 的 Toolkit 均含 bocha_search loader
3. **False 场景**：`search_enabled=False` → all_skills_meta 无 bocha_search → 三个 agent 均无 bocha_search（行为不变）
4. **skills 叠加**：同时传 `skills=["mineru"]` + `search_enabled=True` → extra = ["mineru", "bocha_search"] → 每个 agent 含两者
5. **向后兼容**：不传 search_enabled → 默认 True → 每个 agent 有 bocha_search
6. **git commit & push** 到 `origin/trae/agent-5CYjia`
