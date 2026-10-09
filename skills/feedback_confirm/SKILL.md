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
