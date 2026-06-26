# Tasks

- [x] Task 1: 新增 mng 代理端点
  - `.env` / `.env.example` 添加 `MNG_URL` 配置
  - `app/config.py` 添加 `MNG_URL` 常量
  - 创建 `app/routes/mng_proxy.py`，包含两个 GET 代理端点（/ui/presentation/cards 和 /ui/presentation/custom-components）
  - `app/main.py` 注册 mng_proxy 路由
  - `requirements.txt` 添加 `httpx`

- [x] Task 2: 注册图表/卡片工具到 Agent
  - 分析 `tools/chart_tools.py` 和 `tools/card_config_tools.py` 中的工具函数，删除不需要的工具
  - 确认 `config/skill_config.yml` 包含 `chart_renderer` 和 `card_interaction` 技能路径
  - 验证 AgentScope `load_skills()` 能正确加载这些技能的工具（通过 `LocalWorkspace` + `skill_paths`）

- [x] Task 3: 实现 CUSTOM_COMPONENT SSE 事件
  - 修改 `app/services/chat_service.py` 的 `generate_response`
  - 在 `AgentEvent` 处理循环中，检测工具结果（`event.type` 为 `tool_call_end`）
  - 从工具结果中提取组件 JSON，构建并 yield `CUSTOM_COMPONENT` 事件
  - `CUSTOM_COMPONENT` 事件格式：`data: {"type": "custom_component", "component": {...}}`

- [x] Task 4: 端到端验证
  - mng 代理接口正常转发请求
  - Agent 能调用图表工具并返回结果
  - SSE 流中包含 `CUSTOM_COMPONENT` 事件
  - `TRACE_READY` 事件仍在流末尾正常发出

# Task Dependencies
- Task 1 无依赖
- Task 2 无依赖  
- Task 3 依赖 Task 2
- Task 4 依赖 Task 1~3