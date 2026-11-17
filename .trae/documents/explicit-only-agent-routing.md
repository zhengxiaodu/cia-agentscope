# 显式触发智能体：意图识别自动路由排除方案

## Context

当前用户在前端**不选择智能体**（走默认编排入口）时，意图识别 LLM 会根据提问内容自动选择智能体，包括 mng 下发的深度研究智能体。深度研究是重任务（长耗时、高 token 消耗、多轮搜索），用户只是问普通分析类问题时一旦被误路由，体验和成本都不佳。

目标：**用户没有主动选择深度研究智能体时，意图识别不得自动选中它**；用户显式选择时正常使用（直答路径不受影响）。

## 现状链路（探索结论）

```
用户提问（未选智能体）
  → orchestrator_service.run()
  → _load_config_bundle()          # 登录时 Redis 缓存的融合配置（YAML + mng 外部意图 + 权限过滤）
  → _build_intent_components()     # merged_intents → IntentConfig 列表 → IntentRecognizer
  → IntentRecognizer 意图识别 LLM  # 候选集 = merged_intents 渲染出的意图树
用户提问（已选智能体）
  → _run_single_agent_path()       # 完全绕过意图识别，直接执行
```

关键点：`IntentRecognizer` 的候选意图全部来自 `fused["merged_intents"]`，从候选集中剔除即无法被 LLM 选中。

## 改动方案（3 个文件）

### 1. config/intent_config.yml —— 新增配置段

```yaml
# 显式触发智能体：仅当用户在前端主动选择该智能体时才启用，
# 意图识别（自动路由）候选集中始终排除对应意图。
# 值 = mng /api/intents 返回中 agent.code 字段
explicit_only_agents:
  - <深度研究智能体的 agent code>
```

### 2. app/services/orchestrator_service.py —— 加载配置 + 候选集过滤

- `__init__`（现有第 134 行处已加载 `raw_intent_config`）：同处读取 `explicit_only_agents`，存为 `self._explicit_only_agents: set`（空/缺失视为空集，行为不变）
- `_build_intent_components`（第 336 行）：构建 `intent_configs` 前过滤 `fused["merged_intents"]`，剔除 `item["agent"]` 在集合中的条目

### 3. tests/ —— 新增单测

参考 `tests/test_intent_status_filter.py` 的模式，验证：
- explicit_only 的 agent 对应意图被剔除出候选集
- 非 explicit_only 意图（含基础意图）全部保留
- 配置为空/缺失时行为不变

## 设计要点：为什么在 `_build_intent_components` 过滤

**不在 `merge_external_into_memory`（mng_service.py）中过滤**：合并产物除了 `merged_intents` 还有 `merged_agents` / `merged_skills`（供工作区装配智能体注册表）。若在合并阶段剔除，智能体定义就没了——用户显式选择时直答路径会报"agent 不存在"。

`_build_intent_components` 只服务意图识别，是"模型可见意图列表"的唯一出口，在此过滤刚好实现"自动路由不可见、显式选择可用"。

## 改动后的行为

| 场景 | 行为 |
|---|---|
| 用户未选智能体 | 意图识别 prompt 中无深度研究意图 → LLM 无法选中 → 自动落到 general_chat 等基础意图 |
| 用户显式选择深度研究 | 走 `_run_single_agent_path` 直答路径（不经意图识别）→ 正常使用 |
| 用户未被授权深度研究 | 白名单过滤本来就会剔除（现状不变） |

## 待定信息

深度研究智能体的实际 agent code 存在 mng 侧，本地代码查不到。实施时需要你从 mng 管理界面（或 `/api/intents` 返回）查到实际值填入配置；配置本身是通用列表，后续其他重意图智能体也可直接追加。

## 验证方式

1. 单测：运行新增测试（pytest tests/ -k explicit）
2. 全量回归：pytest 确认无既有用例被破坏
3. 端到端（可选）：AUTH_MOCK 场景下构造含目标 agent 的外部意图，验证意图识别 prompt 中不包含该意图、显式选择直答路径正常
