# /chat 新增 search_enabled 开关控制 bocha_search 联网搜索技能

## Summary

在 `/chat` 请求体中新增布尔参数 `search_enabled`（默认 `True`，向后兼容）。当为 `False` 时，从本次请求的可用技能集合中移除 `bocha_search` 联网搜索技能，使智能体本轮无法调用它；当为 `True` 时保持现有行为（bocha_search 可用）。该开关为**每轮请求级别**，不影响登录时融合缓存的 `merged_skills`，也不影响其他用户/会话。

## Current State Analysis

技能流转链路（基于代码探索，均为绝对路径引用）：

1. **登录融合**：`/login`（[auth.py](file:///workspace/app/routes/auth.py)）调用 `orchestrator.build_and_cache_user_config` → `_fuse_user_config`（[orchestrator_service.py:204](file:///workspace/app/services/orchestrator_service.py)）把基础技能（含 `bocha_search`，来自 [skill_config.yml](file:///workspace/config/skill_config.yml)）与 mng 外部技能融合为 `merged_skills: list[dict]`，每条结构 `{name, directory, description}`，按 `user_id` 写入 Redis（key=`user_config:{user_id}`）。

2. **聊天读取**：`/chat`（[chat.py:13](file:///workspace/app/routes/chat.py)）→ `generate_response`（[chat_service.py:102](file:///workspace/app/services/chat_service.py)）→ `orchestrator_service.run`（[orchestrator_service.py:385](file:///workspace/app/services/orchestrator_service.py)）→ `_build_request_components`（[orchestrator_service.py:294](file:///workspace/app/services/orchestrator_service.py)）从 Redis 读 `merged_skills`。

3. **技能装载**：`_build_request_components` 提取 `all_skill_dirs = [s["directory"] for s in merged_skills]`（[orchestrator_service.py:335](file:///workspace/app/services/orchestrator_service.py)）→ 创建/复用 Docker workspace（按 `(user_id, session_id)` 缓存，[orchestrator_service.py:338-344](file:///workspace/app/services/orchestrator_service.py)）→ `all_skills_meta = await workspace.list_skills()`（[orchestrator_service.py:352](file:///workspace/app/services/orchestrator_service.py)）→ 喂入 `AgentRegistry`。

4. **智能体绑定**：`AgentRegistry._build_toolkit_for`（[registry.py:70-91](file:///workspace/app/agents/registry.py)）按 `general_agent.skills=["bocha_search"]`（[agent_config.yml](file:///workspace/config/agent_config.yml)）从 `all_skills_meta` 中按 `name` 过滤，装入 Toolkit。**`all_skills_meta` 是智能体实际可见技能的唯一来源**——若其中不含 `bocha_search`，任何智能体都无法使用它。

5. **ChatRequest 现状**（[chat.py:5-8](file:///workspace/app/models/chat.py)）：仅 `messages / session_id / agent_id` 三字段，无技能相关字段。

**关键约束**：workspace 按 `(user_id, session_id)` 缓存复用。若直接从 `merged_skills`/`all_skill_dirs` 移除 bocha_search，则首次 `search_enabled=False` 创建的 workspace 不含该技能，后续 `search_enabled=True` 时缓存的 workspace 仍无该技能 → 搜索无法恢复（BUG）。因此过滤必须发生在 `all_skills_meta` 层（workspace 始终装载全部技能，每轮请求按开关显隐），而**不能**改动 `merged_skills`/`all_skill_dirs`。

## Proposed Changes

### 1. 扩展请求模型 — [app/models/chat.py](file:///workspace/app/models/chat.py)
在 `ChatRequest` 新增字段：
```python
class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    search_enabled: bool = True   # 默认 True 向后兼容；False 时移除 bocha_search
```
**Why**：用户要求的入参开关。默认 `True` 保持存量前端行为不变（用户已确认）。

### 2. 透传开关 — [app/routes/chat.py](file:///workspace/app/routes/chat.py)
在调用 `generate_response` 时（约 30-39 行）追加 `search_enabled=body.search_enabled`。
**Why**：把请求体字段传入服务层。`request` 对象已透传，无需额外传 app.state。

### 3. 透传开关 — [app/services/chat_service.py](file:///workspace/app/services/chat_service.py)
- `generate_response` 签名（102-111 行）新增参数 `search_enabled: bool = True`。
- 调用 `orchestrator_service.run(...)`（164-171 行）时追加 `search_enabled=search_enabled`。
**Why**：纯透传，不在适配层做任何技能逻辑。

### 4. 透传开关 + 执行过滤 — [app/services/orchestrator_service.py](file:///workspace/app/services/orchestrator_service.py)
- 模块顶部新增常量：`_SEARCH_SKILL_NAME = "bocha_search"`（与 [skill_config.yml](file:///workspace/config/skill_config.yml) 的 `name`、[agent_config.yml](file:///workspace/config/agent_config.yml) 的绑定名一致）。
- `run` 签名（385-393 行）新增 `search_enabled: bool = True`。
- `run` 内调用 `_build_request_components`（424-430 行）时追加 `search_enabled=search_enabled`。
- `_build_request_components` 签名（294-299 行）新增 `search_enabled: bool = True`。
- **过滤点**：在第 352 行 `all_skills_meta = await workspace.list_skills()` 之后，新增过滤逻辑：
  ```python
  all_skills_meta = await workspace.list_skills()

  # 按请求开关显隐联网搜索技能（workspace 始终装载全部技能，此处按轮次过滤）
  if not search_enabled:
      all_skills_meta = [
          m for m in all_skills_meta
          if (getattr(m, "name", None) or (m.get("name") if isinstance(m, dict) else None))
             != _SEARCH_SKILL_NAME
      ]
  ```
  （`meta.name` 提取方式与 [registry.py:85-87](file:///workspace/app/agents/registry.py) 保持一致，兼容对象/dict 两种形态。）
- **不改动** `merged_skills`、`all_skill_dirs`、workspace 创建逻辑。

**Why**：
- 在 `all_skills_meta` 层过滤是唯一能同时满足"True 时可用 / False 时不可用"且兼容 workspace 缓存的位置。
- 由于 [registry.py:88](file:///workspace/app/agents/registry.py) 按 `name in bound_skill_names` 从 `all_skills_meta` 过滤，`all_skills_meta` 中移除 bocha_search 后，`general_agent` 的 Toolkit 自然不含该技能 → 本轮无法调用搜索，达到用户目标。
- 不动 `merged_skills`：保证 workspace 始终按完整技能列表创建/复用，`search_enabled` 在 True↔False 间切换时缓存的 workspace 始终持有 bocha_search，仅按请求显隐，无 BUG。

## Assumptions & Decisions

1. **默认 `True`**：用户已确认，向后兼容存量前端。
2. **仅影响 bocha_search**：用户明确"联网搜索技能"即 bocha_search；当前系统只有这一个搜索技能（[skill_config.yml](file:///workspace/config/skill_config.yml)）。未来若有多个搜索技能，可扩展常量为集合。
3. **过滤层选 `all_skills_meta` 而非 `merged_skills`**：因 workspace 按 `(user_id, session_id)` 缓存复用（[orchestrator_service.py:338](file:///workspace/app/services/orchestrator_service.py)），改 `merged_skills`/`all_skill_dirs` 会导致 False→True 切换时缓存的 workspace 缺技能无法恢复。`all_skills_meta` 是每轮重新 `workspace.list_skills()` 得到，是智能体可见技能的唯一真实来源，在此过滤既正确又最小。
4. **不修改 Redis 缓存 / 登录流程 / agent_config 绑定**：开关是每轮请求级，不持久化、不影响其他会话。
5. **单 agent 直接问答路径同样生效**：`agent_id` 直答路径（[orchestrator_service.py:433](file:///workspace/app/services/orchestrator_service.py)）也使用同一 `registry`/`agent_factory`，过滤对其同样有效，无需额外处理。
6. **不引入新依赖**：仅复用现有类型。
7. **`meta.name` 提取兼容对象/dict**：与 registry 现有写法一致，避免 SDK 返回类型变化导致报错。

## Verification

1. **静态检查**：`python -m py_compile app/models/chat.py app/routes/chat.py app/services/chat_service.py app/services/orchestrator_service.py` 全部通过。
2. **字段核对**：`ChatRequest` 含 `search_enabled: bool = True`；grep 确认 `search_enabled` 在 chat.py → chat_service.py → orchestrator_service.py 的 run → `_build_request_components` 透传链完整。
3. **过滤逻辑核对**：`_build_request_components` 中 `if not search_enabled:` 分支过滤 `all_skills_meta`，且 `merged_skills`/`all_skill_dirs` 未被修改。
4. **行为核对**：
   - `search_enabled=True`（含默认）：`all_skills_meta` 保留 bocha_search，general_agent Toolkit 含搜索技能。
   - `search_enabled=False`：`all_skills_meta` 不含 bocha_search，任何 agent Toolkit 均无搜索技能。
5. **缓存兼容核对**：确认 workspace 创建逻辑（338-344 行）未改动，`all_skill_dirs` 仍来自完整 `merged_skills`，False→True 切换可恢复。
