# 登录/注册返回时合并"制度问答"类智能体为统一 regulations\_qa

## Summary

登录/注册（以及 /refresh、/api/auth/me/\*）返回给前端的 `agent_access` 中，凡名称包含"制度问答"的智能体（如"金科制度问答"、"信科制度问答"、"制度问答"）合并为一个 `{"id": "regulations_qa", "name": "制度问答", "description": "根据公司内部制度文件知识库回答问题"}` 项。Redis 中存储的 permissions 保持原始不变（policy\_qa 工具按原始名称映射知识库 ID，依赖原名）。

## Current State Analysis（基于实际代码）

- [auth.py](file:///workspace/app/routes/auth.py) 中有 4 处返回 `agent_access`：

  - `_build_auth_success`（login/register 共用，L143）：`permissions["agent_whitelist"]`

  - `_build_update_response`（me/name|department|password 共用，L190 附近）：`permissions.get("agent_whitelist", [])`

  - `refresh_token`（L263 附近）：`permissions.get("agent_whitelist", [])`

- `agent_whitelist` 元素结构 `{"id","name","show",...}`（可能带上一轮注入的 `description`），来自 mng。

- 上一轮功能 `_enrich_agent_access` 在存 Redis 前为 whitelist 注入 description——本次合并**只在响应构造时**做，不动 Redis。

- [policy\_qa\_tools.py](file:///workspace/tools/policy_qa_tools.py) `_resolve_kb_ids` 按 whitelist 项的 `name` 匹配 `POLICY_QA_KB_MAP`（"金科制度问答"→kb），故 Redis 必须保留原始项。

- 既有测试 [tests/test\_agent\_access\_description.py](file:///workspace/tests/test_agent_access_description.py) 覆盖 `_build_auth_success`。

## Proposed Changes

### 1. \[app/routes/auth.py] 新增 `_merge_regulations_agents`

```python
# 制度问答类智能体合并：名称含该关键字的多个智能体（金科/信科制度问答等）
# 在返回前端时统一为一个入口，id 固定为 regulations_qa。
_REGULATIONS_KEYWORD = "制度问答"
_MERGED_REGULATIONS_AGENT = {
    "id": "regulations_qa",
    "name": "制度问答",
    "description": "根据公司内部制度文件知识库回答问题",
}


def _merge_regulations_agents(agent_whitelist: list) -> list:
    """将名称包含"制度问答"的智能体合并为单个 regulations_qa 项。

    仅用于返回前端的 agent_access；Redis permissions 保持原始不变
    （policy_qa 按原始名称映射知识库 ID）。
    无命中时原样返回；合并项插入到首个命中原项的位置。
    """
```

实现要点：

- 遍历 list，`isinstance(item, dict)` 且 `_REGULATIONS_KEYWORD in (item.get("name") or "")` 的项被收拢

- 有命中：返回 `[非命中项...]`，并在**首个命中原项的索引位置**插入 `dict(_MERGED_REGULATIONS_AGENT)`（浅拷贝，防止调用方修改污染常量）

- 无命中：返回原 list（不拷贝）

- 纯函数，不修改入参

### 2. \[app/routes/auth.py] 4 处响应统一套用

- `_build_auth_success`：`"agent_access": _merge_regulations_agents(permissions["agent_whitelist"])`

- `_build_update_response`：`"agent_access": _merge_regulations_agents(permissions.get("agent_whitelist", []))`

- `refresh_token`：同上

- 注意：均在**构造响应 dict 时**转换，`save_user_permissions` 仍存原始 `permissions`（enrich 后未合并的），顺序不变

### 3. \[tests/test\_agent\_access\_description.py] 追加测试（复用既有文件，同属 agent\_access 构造逻辑）

- `_merge_regulations_agents` 单元测试：

  - 金科+信科两个 → 合并为 1 个 `regulations_qa`，位置在首个命中原项处，其余非制度项保留原顺序

  - 无命中 → 原样返回（同一对象或相等）

  - 空列表 / 非 dict 项健壮性

  - 返回项不被常量污染（修改返回项不影响 `_MERGED_REGULATIONS_AGENT`）

- `_build_auth_success` 集成：在既有 `_RESULT` 基础上构造含 `金科制度问答`/`信科制度问答`/`制度问答` 三个项的 whitelist（deepcopy 修改），断言：

  - 响应 `agent_access` 中制度类只剩 1 项且 id=regulations\_qa、description 为统一文案

  - Redis `save_user_permissions` 收到的仍是**未合并**的原始 whitelist（3 项都在）

## Assumptions & Decisions

- 识别规则：name 包含"制度问答"子串（用户已确认）

- 合并项 description 固定为"根据公司内部制度文件知识库回答问题"（用户指定统一文案，不取意图 definition）

- 合并项不保留 `show` 等其他字段（前端展示入口用统一项）

- 转换只发生在响应构造层；Redis、policy\_qa 权限映射、意图融合全链路不受影响

- `/register` 共用 `_build_auth_success` 自动生效

## Verification

1. `python -m pytest tests/test_agent_access_description.py -q` 全过
2. `python -m pytest tests/ -q` 全量回归无破坏
3. 手动冒烟：whitelist 含金科/信科制度问答时响应形如：
   `agent_access: [..., {"id": "regulations_qa", "name": "制度问答", "description": "根据公司内部制度文件知识库回答问题"}, ...]`

