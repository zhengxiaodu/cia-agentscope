# feedback_confirm 技能创建与注册计划

## 概述

创建一个调用反馈卡片工具的技能 `feedback_confirm`：智能体在需要用户**二选一**时，调用 `render_feedback_card` 工具发送 `feedback_confirm` 类型交互卡片并同轮等待用户选择。技能完成后注册到 `skill_config.yml` 并绑定到通用问答智能体（general_agent）。

## 现状分析（探索结论）

1. **工具层已就绪，无需改动**：
   - [feedback_tools.py](file:///workspace/tools/feedback_tools.py) 提供 `create_feedback_tool(session_id)` 工厂，产出名为 `render_feedback_card` 的 FunctionTool
   - [workspace_assembler.py](file:///workspace/app/services/workspace_assembler.py) L96/L129 已将其注入每会话 `all_tools`（对所有智能体全局可见）
   - 工具签名：`render_feedback_card(card_type, schema, title="")`；行为：发卡 → 挂起等待 `POST /chat/feedback` 回传 → 返回结果供模型同轮继续推理
   - 回传结果（[chat.py](file:///workspace/app/routes/chat.py) L75-80、[feedback_service.py](file:///workspace/app/services/feedback_service.py)）：
     - 成功：`{"action": "confirm|cancel|submit", "payload": {...}}`
     - 异常：`{"status": "timeout|cancelled|error", "message": "..."}`
2. **组件配置已就绪**：`config/mock/presentation_custom_components.json` 已有 `componentType=feedback_confirm` 的 mock 配置（schemaFields 含 title/subtitle/options）
3. **技能目录约定**（参照 [card_interaction](file:///workspace/skills/card_interaction/SKILL.md)）：
   - `SKILL.md`：frontmatter（name / description / license / metadata.source / metadata.tools）+ 正文（功能描述、入参格式、参数说明、返回格式、使用示例、注意事项）
   - `tools.py`：导入技能依赖的工具模块（card_interaction 模式）
4. **注册与绑定机制**：
   - `skill_config.yml` 条目的 `directory` → `create_workspace(skill_dirs=...)` 将技能目录注入沙箱
   - 沙箱内 `/workspace/skills/` 按**目录名**扫描技能元信息（`list_skills`）
   - `agent_config.yml` 中 `agent.skills` 按名字匹配筛选技能子集（[registry.py](file:///workspace/app/agents/registry.py) `_build_toolkit_for`）
   - **关键约束：技能名必须与目录 basename 完全一致** → 目录名定为 `feedback_confirm`

## 改动方案（共 4 项：2 新建 + 2 修改）

### 1. 新建 `/workspace/skills/feedback_confirm/SKILL.md`

完整内容如下（执行时直接写入）：

````markdown
---
name: feedback_confirm
description: |
  二选一确认卡片技能：渲染 feedback_confirm 交互卡片并暂停等待用户选择，收到反馈后同轮继续处理。
  Use when: 需要用户在两个明确选项中做出选择（二选一确认、办理方式选择、方案取舍等场景）。
license: MIT
metadata:
  source: skill-dir
  tools:
    - render_feedback_card
---

# 二选一确认卡片 Skill

## 功能描述

本技能用于渲染二选一确认卡片（card_type=feedback_confirm）。调用工具后：

1. 卡片立即发送给用户（含标题、副标题、两个选项按钮）
2. 工具挂起等待用户点击选择（默认 300 秒超时）
3. 用户选择后，工具返回选择结果，智能体在同一轮对话内继续推理

## 可用工具

### render_feedback_card - 二选一确认卡片渲染工具

**适用场景**: 需要用户从两个选项中选择一项，且后续处理依赖用户的选择结果。

**入参格式**:
```json
{
    "card_type": "feedback_confirm",
    "schema": {
        "title": "示例卡片",
        "subtitle": "请选择以下方式",
        "options": [
            {"label": "选项A...", "value": "chose_a"},
            {"label": "选项B...", "value": "chose_b"}
        ]
    },
    "title": "示例卡片"
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| card_type | string | 是 | 固定为 "feedback_confirm" |
| schema | object | 是 | 卡片数据：title（卡片标题）、subtitle（副标题/引导语）、options（选项数组，每项含 label 显示文案与 value 选项标识） |
| title | string | 否 | 卡片标题，优先级高于 schema.title |

**返回格式**:
- 用户已选择：`{"action": "confirm", "payload": {...}}`（payload 含用户所选选项信息）
- 用户取消：`{"action": "cancel", "payload": {}}`
- 等待超时：`{"status": "timeout", "message": "用户未在 300 秒内反馈"}`
- 其他异常：`{"status": "cancelled|error", "message": "..."}`

**选择条件**:
- 用户需要在两个明确选项中二选一（如选择办理方式、确认/取消某操作）
- 后续流程依赖用户的选择结果

**不适用场景**:
- 三个及以上选项的列表选择（使用 render_selectable_list）
- 高风险金融操作的详情二次确认（使用 render_confirm_action）
- 无需用户交互的只读信息展示（使用 render_generic_card）

## 使用示例

**用户输入**: "我要办理这项业务，有两种方式，帮我选一下"

**调用工具**: `render_feedback_card`

**工具参数**:
```json
{
    "card_type": "feedback_confirm",
    "schema": {
        "title": "示例卡片",
        "subtitle": "请选择以下方式",
        "options": [
            {"label": "选项A...", "value": "chose_a"},
            {"label": "选项B...", "value": "chose_b"}
        ]
    },
    "title": "示例卡片"
}
```

**收到反馈后**: 根据 action/payload 同轮继续处理——返回 confirm 时按用户所选选项给出对应后续回答；返回 cancel 时确认用户放弃并停止后续流程。

## 注意事项

1. **必须调用工具**: 需要用户二选一时必须调用 render_feedback_card，不要直接输出文字让用户回复
2. **card_type 固定**: 必须传 "feedback_confirm"，否则前端无法渲染对应组件
3. **options 恰好两项**: 本卡片面向二选一场景，options 数组应包含且仅包含 2 个选项
4. **label/value 对应**: label 为用户可见文案，value 为程序化标识，用户选择后以 value 回传
5. **同轮恢复**: 工具返回后智能体在同一轮内继续推理，应根据 payload 中用户的选择直接给出后续回答，不要重复发问
6. **超时处理**: 返回 status=timeout 时应友好告知用户等待超时，请用户重新发起
7. **不要连续发卡**: 同一会话同时只允许一张待反馈卡片，收到反馈前不要再次调用本工具
````

### 2. 新建 `/workspace/skills/feedback_confirm/tools.py`

```python
from tools.feedback_tools import create_feedback_tool
```

说明：`render_feedback_card` 是宿主侧工厂闭包（注入 session_id，见 feedback_tools.py L62-78），无法直接导入函数本身，故按 card_interaction 的声明式导入模式导入工厂函数，表明技能依赖。

### 3. 修改 `/workspace/config/skill_config.yml`

在 `skills` 列表末尾（policy_qa 条目之后）追加：

```yaml
  - name: feedback_confirm
    directory: ./skills/feedback_confirm
    description: "二选一确认卡片技能：渲染 feedback_confirm 交互卡片并暂停等待用户选择，收到反馈后同轮继续处理。"
```

### 4. 修改 `/workspace/config/agent_config.yml`

- `general_agent` 的 `skills` 列表追加 `feedback_confirm`：

```yaml
  - id: general_agent
    name: 通用问答智能体
    skills:
      - bocha_search
      - feedback_confirm
```

- `general_agent` 的 `system_prompt` 追加一条使用指引（现有 prompt 已按技能写使用规则——搜索技能即此模式，新技能需要触发指引模型才会正确调用）。在"如果用户上传了文件…"条目之后追加：

```
      - 如果需要用户在两个选项中做出选择（二选一确认），调用 render_feedback_card 工具发送确认卡片（card_type=feedback_confirm），等待用户选择后根据返回的 action/payload 继续处理，不要重复发问
```

## 假设与决策

1. **不动工具层/服务层**：`render_feedback_card` 已在 workspace_assembler 全局注入，本次仅新增技能层文件与两处 YAML 配置。
2. **目录名 = 技能名 = 绑定名**：`feedback_confirm` 三处必须一致（沙箱按目录 basename 识别技能，registry 按名匹配）。
3. **tools.py 导入工厂**而非工具函数：闭包函数不可直接导入，且与用户此前确认的方案一致。
4. **system_prompt 补一行**：符合 general_agent 现有 prompt 按技能写规则的惯例，保证模型有触发时机指引；改动最小（仅一条）。

## 验证步骤

1. **YAML 语法校验**：`python3 -c "import yaml; yaml.safe_load(open('config/skill_config.yml')); yaml.safe_load(open('config/agent_config.yml')); print('OK')"`
2. **配置一致性**：确认 skill_config.yml 的 `name` == 目录 basename == agent_config.yml 绑定名 == SKILL.md frontmatter `name`，均为 `feedback_confirm`
3. **tools.py 语法编译**：`python3 -m py_compile skills/feedback_confirm/tools.py`（宿主环境无 agentscope，仅做语法校验，不做 import）
4. **目录结构核对**：`skills/feedback_confirm/` 下含 `SKILL.md` 与 `tools.py`，与其他技能（card_interaction 等）结构一致
