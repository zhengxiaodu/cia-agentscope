# 管理中心代理 & 图表渲染技能集成 Spec

## Why

1. 前端需要从管理中心（mng）获取通用卡片配置和自定义卡片配置，直接暴露 mng 地址存在安全风险，需要在后端做代理转发
2. Agent 当前已有搜索、工具调用能力，但缺乏图表渲染能力。sunny 项目提供了一套完整的图表/卡片渲染工具链（chart_renderer + card_interaction 技能），需要集成到 Agent 中，让 Agent 根据数据特征自动选择最合适的图表类型，并将工具结果作为 `CUSTOM_COMPONENT` 事件通过 SSE 流返回给前端渲染

## What Changes

### 新增
- `.env` / `.env.example`：新增 `MNG_URL` 配置项
- `app/config.py`：新增 `MNG_URL` 读取逻辑
- `app/routes/mng_proxy.py`：GET `/ui/presentation/cards` 和 `/ui/presentation/custom-components` 代理端点
- `requirements.txt`：新增 `httpx`

### 注册 Chart / Card 工具到 Agent
- `config/skill_config.yml`：新增 `chart_renderer` 和 `card_interaction` 技能路径
- AgentScope `LocalWorkspace` 通过 `skill_paths` 自动发现 `tools/chart_tools.py` 和 `tools/card_config_tools.py` 中的工具函数

### 修改
- `app/services/chat_service.py`：在 `generate_response` 中解析工具执行结果，当检测到图表/卡片类工具输出时，额外 yield 一条 `CUSTOM_COMPONENT` SSE 事件
- `app/main.py`：注册 mng_proxy 路由

## Impact
- Affected specs: `app/routes/`, `app/services/chat_service.py`, `config/`, `.env`, `requirements.txt`
- Affected code:
  - `.env` / `.env.example` / `app/config.py`：新增 `MNG_URL`
  - `app/main.py`：注册 mng_proxy 路由
  - `requirements.txt`：新增 `httpx`

## ADDED Requirements

### Requirement: 管理中心代理接口

The system SHALL 提供两个代理接口，将前端请求转发到 mng 管理系统。

#### Scenario: 获取通用卡片配置
- **WHEN** 前端请求 `GET /ui/presentation/cards`
- **THEN** 后端使用 `httpx.AsyncClient` 将请求转发到 `{MNG_URL}/ui/presentation/cards`，返回 mng 的 JSON 响应

#### Scenario: 获取自定义卡片配置
- **WHEN** 前端请求 `GET /ui/presentation/custom-components`
- **THEN** 后端转发到 `{MNG_URL}/ui/presentation/custom-components`，返回 mng 的 JSON 响应

### Requirement: Chart / Card 工具注册到 Agent Task 2

The system SHALL 将 chart_renderer 和 card_interaction 两个技能的 tools 注册到 AgentScope 的 Toolkit 中。

#### Scenario: Agent 启动时发现工具
- **WHEN** 后端启动，`load_skills()` 初始化 LocalWorkspace
- **THEN** LocalWorkspace 扫描 skill_paths 中的 SKILL.md，自动发现并注册以下工具：
  - 图表渲染工具：`render_line_chart`、`render_bar_chart`、`render_pie_chart`、`render_tech_indicator_table`、`render_indicator_card`
  - 卡片配置工具：`get_card_config`、`get_custom_component_config`
  - 卡片交互工具：`render_general_card`、`render_selectable_list`、`render_confirmation_dialog`

### Requirement: CUSTOM_COMPONENT SSE 事件

The system SHALL 在工具执行完毕后，解析工具结果中的图表/卡片数据，将其作为 `CUSTOM_COMPONENT` 事件通过 SSE 流返回。

#### Scenario: 图表工具调用完成
- **WHEN** Agent 调用 `render_*` 图表工具执行完毕，工具结果包含 JSON 格式的组件数据
- **THEN** 在 `generate_response` 中，解析 `AgentEvent` 中的 `TOOL_RESULT_END` 事件，提取组件 JSON，构建 `CUSTOM_COMPONENT` 事件结构并 yield 到 SSE 流：
  ```json
  data: {"type": "custom_component", "component": {"chartType": "line", "title": "趋势图", "data": [...]}}
  ```

### Requirement: 前端获取 trace_id + 组件渲染

The system SHALL 保留现有的 `TRACE_READY` 事件，`CUSTOM_COMPONENT` 事件在 `TRACE_READY` 之前发出（位于流中间）。

## MODIFIED Requirements

### Requirement: `/chat` SSE 流（增强）

在 `generate_response` 中，原逻辑是 yield 所有 AgentEvent。增强后，当 AgentEvent 的 `data` 包含工具执行结果时：
1. 先 yield 原始 `AgentEvent`（保持现有行为）
2. 再 yield 一个 `CUSTOM_COMPONENT` 事件，包含解析后的组件 JSON

## REMOVED Requirements

无