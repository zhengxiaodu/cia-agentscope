# orchestrator_service.py 瘦身重构计划

## 背景与目标

[orchestrator_service.py](file:///workspace/app/services/orchestrator_service.py) 已膨胀到 **1274 行**，混杂了 4 类与"编排流程"无关的支撑逻辑。目标：把可独立的关注点外移为新模块，让该文件回归"编排流程 + 服务装配"本职（预计降到 ~700 行），**行为零变化**（方法签名、SSE 事件序列、日志文案、降级路径全部保持）。

## 现状分析

文件内职责分块（行号基于当前版本）：

| 块 | 行数 | 内容 | 对 self 的依赖 |
|---|---|---|---|
| 用户配置融合/缓存 | L340-476 | `_fuse_user_config`、`build_and_cache_user_config`、`_load_cached_user_config`、`_load_config_bundle` + Redis key/TTL 常量 | 无（纯函数，只用到模块导入） |
| 工作区装配 | L501-644 | `_prepare_workspace_components`（工具/技能/注册表组装，144 行） | 仅 `self._workspace_manager`、`self._create_model_fn` |
| AgentState 存取 | L655-731 | `_migrate_legacy_enum_values`、`_trim_state_context`、`_load_agent_state`、`_persist_agent_state` + `_HISTORY_KEEP_LAST` | 无 |
| 上传文件上下文 | L978-1092 | `_wait_for_upload_parsing`、`_load_upload_context`、`_append_upload_context`、`_has_unbound_uploads` + 6 个常量 | 无（只读 `request.app.state.upload_file_dao`） |
| 核心流程（保留） | ~500 行 | `run`、`_run_with_workspace_task`、`_run_single_agent_path`、`create`/`__init__`/属性/小工具 | — |

**外部调用方（公共 API，不可破坏）：**
- [main.py](file:///workspace/app/main.py#L91)：`OrchestratorService.create()`
- [auth.py](file:///workspace/app/routes/auth.py#L195)：`orchestrator_service.build_and_cache_user_config(...)`（且 [test_agent_access_description.py](file:///workspace/tests/test_agent_access_description.py#L147) 在 SimpleNamespace 上 mock 此方法 → **必须保留为实例方法**）
- [chat_service.py](file:///workspace/app/services/chat_service.py#L610)：`run()`、`last_agent_ids`、`last_success`、`last_input_tokens`、`last_output_tokens`

**测试接缝约束（关键）：**
- [test_orchestrator_parallel_workspace.py](file:///workspace/tests/test_orchestrator_parallel_workspace.py#L96-L99) 在实例上打桩 `svc._load_config_bundle` / `svc._build_intent_components` / `svc._prepare_workspace_components` / `svc._create_orchestrator` → **run() 流程必须经 `self.` 调用这 4 个方法**，它们保留在类上
- [test_orchestrator_component_split.py](file:///workspace/tests/test_orchestrator_component_split.py#L94-L107) monkeypatch `app.services.orchestrator_service.{AGENT,INTENT,SKILL}_CONFIG_PATH` → 融合逻辑移走后 **patch 目标必须改为新模块**
- [test_upload_inject_and_bind.py](file:///workspace/tests/test_upload_inject_and_bind.py)（~25 处）、[test_agent_state_persistence.py](file:///workspace/tests/test_agent_state_persistence.py)（~17 处）直调内部方法 → **测试需同步改为从新模块导入**（纯机械替换）

## 拆分方案

### 新模块 1：`app/services/user_config_service.py`（~150 行）

迁移内容（代码原样搬移，仅去掉 `self`）：
- 常量 `_REDIS_KEY_USER_CONFIG`、`_USER_CONFIG_TTL`（含 `JWT_REFRESH_EXPIRE_DAYS` 导入）
- `_fuse_user_config` → `fuse_user_config(jwt_token, permissions)`
- `build_and_cache_user_config(user_id, jwt_token, permissions, redis_client)` → 模块级函数
- `_load_cached_user_config` → `load_cached_user_config(user_id, redis_client)`
- `_load_config_bundle` → `load_config_bundle(user_id, redis_client)`
- 随迁导入：`yaml`、`AGENT_CONFIG_PATH`/`INTENT_CONFIG_PATH`/`SKILL_CONFIG_PATH`/`EXTERNAL_SKILLS_DIR`、`load_agent_definitions`、`load_intent_config`、mng_service 三函数
- 日志 tag 保持 `[OrchestratorService]` 原文不改（最小化行为差异）

类上保留两个薄委托（auth.py / parallel_workspace 桩 / component_split 测试均依赖）：
```python
async def build_and_cache_user_config(self, user_id, jwt_token, permissions, redis_client):
    return await user_config_service_build_and_cache(user_id, jwt_token, permissions, redis_client)

async def _load_config_bundle(self, user_id, redis_client):
    return await load_config_bundle(user_id, redis_client)
```
类上删除：`_fuse_user_config`、`_load_cached_user_config`（仅内部使用，无测试引用）。

### 新模块 2：`app/services/agent_state_store.py`（~110 行）

- 常量 `_HISTORY_KEEP_LAST` 随迁
- `_migrate_legacy_enum_values` → `migrate_legacy_enum_values(state_dict)`
- `_trim_state_context` → `trim_state_context(state_dict, keep_last=_HISTORY_KEEP_LAST)`
- `_load_agent_state` → `load_agent_state(session_service, session_id, agent_id)`
- `_persist_agent_state` → `persist_agent_state(session_service, session_id, user_id, agent_id, state_dict)`
- 类上**删除**这 4 个方法；`_run_single_agent_path` 与 `_run_with_workspace_task` 直接调模块函数
- 导入 `agentscope.state.AgentState`（反序列化用）

### 新模块 3：`app/services/upload_context_provider.py`（~150 行）

- 6 个常量随迁：`_UPLOAD_CTX_HEADER`、`_UPLOAD_CTX_MAX_CHARS`、`_UPLOAD_WAIT_TIMEOUT`、`_UPLOAD_WAIT_POLL_INTERVAL`、`_UPLOAD_PARSE_TIMEOUT_HINTS`、`_UPLOAD_PARSE_TIMEOUT_HINT_DEFAULT`
- `_wait_for_upload_parsing` → `wait_for_upload_parsing(request, session_id)`（async generator）
- `_load_upload_context` → `load_upload_context(request, session_id)`
- `_append_upload_context` → `append_upload_context(text, upload_ctx)`
- `_has_unbound_uploads` → `has_unbound_uploads(request, session_id)`
- 类上**删除**；流程两处（单 agent 短路路径 + 编排路径）直接调函数
- 随迁导入：`asyncio`、`time`、`uuid`、`ToolCallStartEvent`/`ToolCallEndEvent`

### 新模块 4：`app/services/workspace_assembler.py`（~180 行）

- `_prepare_workspace_components` 的方法体 → `assemble_workspace_components(workspace_manager, create_model_fn, fused, user_id, redis_client, user_id_safe, session_id_safe, search_enabled=True, skills=None, langfuse_service=None)`
- `self._workspace_manager` → 参数 `workspace_manager`；`self._create_model_fn` → 参数 `create_model_fn`（传可调用本身，与现状 `create_model_fn=self._create_model_fn` 一致）
- **函数内延迟导入原样保留**（chart_tools、FunctionTool、md_export、policy_qa、base64、adapter、bridge、bocha——避免引入新的模块级循环导入）
- 本地自带 3 行 `_noop_ctx`（与 chat_service 现状对称，不为此建共享 utils）
- 类上保留薄委托 `_prepare_workspace_components`（parallel_workspace 桩 + component_split 测试调用），委托转发全部参数

### orchestrator_service.py 保留结构（~700 行）

```
模块 docstring + 精简 imports
_noop_ctx / _safe_update_span
class OrchestratorService:
    __init__ / create（生命周期）
    _create_model_fn / _create_orchestrator（装配）
    5 个 last_* 属性（对外契约）
    _capture_tokens_from_stream（token 拦截，读写 self 状态）
    _event / _extract_last_user_message / _extract_history（静态小工具）
    _span / _resolve_workspace_task
    薄委托×3：build_and_cache_user_config、_load_config_bundle、_prepare_workspace_components
    _build_intent_components（原样保留：仅 20 行且依赖 self._intent_client/_prompts）
    _run_single_agent_path（核心流程）
    run（入口）
    _run_with_workspace_task（核心流程）
```

**imports 清理**（迁移后 grep 逐一确认再删）：`yaml`、`uuid`、`time`、`SKILL_CONFIG_PATH`、`EXTERNAL_SKILLS_DIR`、`JWT_EXPIRE_HOURS`、`JWT_REFRESH_EXPIRE_DAYS`、`load_agent_definitions`、`fetch_external_intents`、`merge_external_into_memory`、`build_agent_definition_map`、`ToolCallStartEvent`/`ToolCallEndEvent`、`ReplyStartEvent`（若无残余引用）。保留 `AGENT_CONFIG_PATH`? → 否（只随融合走）；保留 `INTENT_CONFIG_PATH`（`create()` 读 orchestrator 参数仍用）。

### 测试更新（3 个文件，纯机械替换）

1. **test_orchestrator_component_split.py**：`_patch_config_paths` 的 3 处 monkeypatch 目标 `app.services.orchestrator_service.*_CONFIG_PATH` → `app.services.user_config_service.*_CONFIG_PATH`；其余 `svc._load_config_bundle` / `svc._build_intent_components` / `svc._prepare_workspace_components` 调用经委托不变
2. **test_upload_inject_and_bind.py**：`_make_service()._load_upload_context(...)` → `load_upload_context(...)`（~25 处，含 `_wait_for_upload_parsing`、`_has_unbound_uploads`、`OrchestratorService._append_upload_context`），改为 `from app.services.upload_context_provider import ...`
3. **test_agent_state_persistence.py**：`svc._load_agent_state(...)` → `load_agent_state(...)`（11 处）、`OrchestratorService._migrate_legacy_enum_values(...)` → `migrate_legacy_enum_values(...)`（6 处），改为 `from app.services.agent_state_store import ...`

**无需改动**：test_orchestrator_parallel_workspace（实例桩仍有效）、test_orchestrator_token_capture、test_agent_access_description（mock 的实例方法委托保留）、test_orchestrator_component_split 其余部分。

## 假设与决策

1. **单 agent 路径（`_run_single_agent_path`，144 行）不外移**：它读写 `self._last_agent_ids`/`_last_success` 并深度使用 `self._span`/`self._event`，是编排流程本体；外移需引入 out-param 改变代码形态，违背"不损害功能逻辑"
2. **4 个接缝方法保留在类上**（`build_and_cache_user_config`、`_load_config_bundle`、`_build_intent_components`、`_prepare_workspace_components`）：既是 run() 流程的扩展点，也是测试打桩点
3. **新模块平铺在 `app/services/` 下**：遵循现有目录惯例（该目录本就是平铺服务文件）；不建子包，避免与 `app/orchestrator/`（编排器实现）概念混淆
4. **日志文案、SSE 事件、降级行为零改动**：所有函数体逐行原样搬移，只做 `self._xxx` → 参数/模块函数的机械替换
5. **不处理既有重复**（如 chat_service 与本文件各自的 `_noop_ctx`）：超出本次范围

## 实施顺序

1. 新建 `agent_state_store.py`（最独立）→ 类中删方法、流程改调用 → 跑 `test_agent_state_persistence.py` 验证
2. 新建 `upload_context_provider.py` → 同上 → 跑 `test_upload_inject_and_bind.py`
3. 新建 `user_config_service.py` → 类上留委托、删 `_fuse_user_config`/`_load_cached_user_config` → 改 component_split 的 monkeypatch 目标 → 跑 `test_orchestrator_component_split.py` + `test_agent_access_description.py`
4. 新建 `workspace_assembler.py` → 类上留委托 → 跑 `test_orchestrator_component_split.py`
5. 清理 orchestrator_service.py 无用 imports → `py_compile` 全部涉及文件
6. 全量回归

## 验证步骤

```bash
# 语法
python -m py_compile app/services/orchestrator_service.py app/services/user_config_service.py app/services/agent_state_store.py app/services/upload_context_provider.py app/services/workspace_assembler.py

# 定向（每步迁移后跑对应文件）
python -m pytest tests/test_agent_state_persistence.py tests/test_upload_inject_and_bind.py tests/test_orchestrator_component_split.py tests/test_orchestrator_parallel_workspace.py tests/test_orchestrator_token_capture.py tests/test_agent_access_description.py -q

# 全量回归
python -m pytest tests/ -q
```

**通过标准**：
- 全量结果与重构前基线一致：508+ passed（本次新增测试后为 518 passed），仅有的 2 个失败仍是 `test_regulations_routes.py` 的 dashboard JWT 鉴权（已验证为预存环境问题，与代码无关）
- `grep -n "def " app/services/orchestrator_service.py` 确认类方法数从 ~30 降到 ~20，文件 ~700 行
- 行为核对：`git diff` 审查所有搬移代码，确认仅存在 `self._xxx` → 参数/函数调用的替换

## 风险与回滚

- **循环导入**：新模块只依赖 app.config / agentscope / mng_service / tools（均为被单向依赖），且 workspace_assembler 的重导入保持函数内延迟——风险低；`py_compile` + 全量测试兜底
- **monkeypatch 失效**（component_split 改目标后仍失败）：说明融合函数读路径的方式有遗漏，检查 `fuse_user_config` 内所有 `*_CONFIG_PATH` 引用是否都从新模块命名空间取
- **回滚**：纯重构无数据/接口变更，`git checkout` 整体还原即可
