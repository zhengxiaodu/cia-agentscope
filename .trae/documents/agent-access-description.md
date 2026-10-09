# 登录返回 agent\_access 附带 description（取自意图 definition）

## Summary

`/login`（及 `/register`）返回的 `agent_access` 中每个 agent 目前没有 `description` 字段。登录流程中本来就会调用 mng 的 `/api/intents`（在 `build_and_cache_user_config` → `_fuse_user_config` → `fetch_external_intents` 中），该接口每个意图带 `definition` 与关联 `agent`。本计划利用这份已有数据，为 `agent_access` 中每个 agent 匹配其对应意图的 `definition`，作为 `description` 字段返回。

## Current State Analysis（基于实际代码）

- [auth.py](file:///workspace/app/routes/auth.py) `_build_auth_success`：`"agent_access": permissions["agent_whitelist"]` 直接透传，无 description。

- `agent_whitelist` 元素结构为 `{"id","name","code"/"show"}`（见 [user\_dao.py](file:///workspace/app/dao/user_dao.py) docstring 与 mock 数据）。

- [mng\_service.py](file:///workspace/app/services/mng_service.py) `merge_external_into_memory` 中白名单匹配键是 **agent 的** **`id`**（`agent_id = ext["agent"]["id"]` ↔ `_build_whitelist_codes` 取 `item["id"]`），因此 description 映射同样按 `intent["agent"]["id"]` ↔ whitelist item `id` 匹配。

- [orchestrator\_service.py](file:///workspace/app/services/orchestrator_service.py) `build_and_cache_user_config` 目前返回 `None`，内部已拿到原始 `external_intents`，但未对外暴露。

- `_build_auth_success` 当前顺序：先 `save_user_permissions` 存 Redis，再 `build_and_cache_user_config`。

- `/refresh`、`/api/auth/me/*` 的响应从 Redis 读 permissions 回填 `agent_access`。

## Proposed Changes

### 1. \[app/services/mng\_service.py] 新增映射构建函数

```python
def build_agent_definition_map(external_intents: list) -> dict:
    """从 /api/intents 原始返回构建 agent_id → definition 映射。

    同一 agent 被多个意图关联时取首个非空 definition；
    跳过非 dict、缺 agent/definition 的意图。
    """
```

纯函数，无 I/O。与 `fetch_external_intents` 放同一模块。

### 2. \[app/services/orchestrator\_service.py] 暴露映射

- `_fuse_user_config` 返回的 dict 增加一项：`"agent_definitions": build_agent_definition_map(external_intents)`（从**原始** external\_intents 构建，不用 merged\_intents，忠实于 /api/intents 的 definition；基础意图不受影响）。

- `build_and_cache_user_config` 返回值从 `None` 改为 fused dict（失败路径仍保证不抛出，返回 None 或降级 dict；现有 try/except 结构保留）。该 dict 会随既有逻辑缓存进 Redis，多余 key 对会话侧读取无影响。

### 3. \[app/routes/auth.py] `_build_auth_success` 重排 + 注入 description

- **重排顺序**（目的：让存入 Redis 的 permissions 也带 description，/refresh、update 接口自动一致）：

  1. 先调 `build_and_cache_user_config(...)` 拿 fused（保持 try/except，失败仅记日志、fused=None）
  2. 新增模块级 helper `_enrich_agent_access(permissions, agent_definitions)`：对 `permissions["agent_whitelist"]` 每项做 `dict(item)` 拷贝，若 `item["id"]` 在映射中则加 `description`；**原地写回** `permissions["agent_whitelist"]`（拷贝项，不污染入参共享对象）
  3. 再 `save_user_permissions`（存的是已 enrich 的 permissions）

- 响应 `"agent_access": permissions["agent_whitelist"]`（此时已带 description）

- 降级：orchestrator 为 None / build 失败 / 映射为空 → 原样返回，登录不受影响（沿用现有"失败不阻断"风格）

- `/register` 共用此函数自动生效；`/refresh`、`/api/auth/me/*` 从 Redis 读到 enriched permissions，自动带 description

### 4. \[tests/test\_agent\_access\_description.py] 新增测试（新建文件）

- `build_agent_definition_map`：正常映射、缺 agent/definition 跳过、非 dict 跳过、同 agent 多意图首个优先、空入参

- `_build_auth_success` 集成：构造 fake Request（`app.state.redis_client` = AsyncMock、`app.state.orchestrator_service` = stub，其 `build_and_cache_user_config` 返回含 `agent_definitions` 的 fused）+ mock `verify_login` 风格的 result dict，断言：

  - 响应 `agent_access` 中匹配项带正确 `description`，未匹配项无该字段

  - 存 Redis 的 permissions 已 enrich（通过 mock redis 的 set 调用参数断言）

  - orchestrator 为 None 时无 description、不报错

## Assumptions & Decisions

- **匹配键**：`intent["agent"]["id"]` ↔ `agent_whitelist` item 的 `id`（与 `merge_external_into_memory` 现有白名单匹配逻辑一致）。

- 基础意图（yml）的 agent 不在 /api/intents 返回中时，对应 whitelist 项不带 description（按用户需求，仅取 /api/intents 的 definition）。

- `definition` 为空字符串的意图不产生映射项。

- permissions 带 `description` 冗余字段存 Redis 对下游（policy\_qa 按名称匹配、merge 按 id 匹配）无影响。

- 不改 `/refresh`、update 接口代码，靠 Redis 中的 enriched permissions 自然生效。

## Verification

1. `python -m pytest tests/test_agent_access_description.py -q` 新用例全过
2. `python -m pytest tests/ -q` 全量回归无破坏
3. 手动冒烟：mock 路径下调用 `_build_auth_success`，确认响应结构：
   `agent_access: [{"id":"999","name":"生成PPT智能体","show":0,"description":"用于生成精美ppt"}, ...]`

