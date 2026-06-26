---
name: card_interaction
description: |
  个性化交互卡片技能，用于展示需要用户交互的个性化卡片组件。
  Use when: 需要用户从列表中选择条目、需要用户确认高风险操作（买卖股票、撤单等）。
license: MIT
metadata:
  source: skill-dir
  tools:
    - render_generic_card
    - render_selectable_list
    - render_confirm_action
---

# 个性化交互卡片 Skill

## 功能描述

本技能用于渲染个性化卡片组件，包括通用卡片、可选列表卡片和操作确认卡片。通用卡片可通过 cardType 映射到管理端配置的渲染模板，实现灵活的卡片展示。

## 可用工具

### 1. render_generic_card - 通用卡片渲染工具

**适用场景**: 需要使用管理端配置的渲染模板展示结构化业务数据

**入参格式**:
```json
{
    "schema": {
        "stockName": "阿里巴巴",
        "price": 85.2,
        "change": "+1.5%"
    },
    "cardType": "stock_overview",
    "title": "股票概览"
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| schema | object | 是 | 卡片业务数据 JSON 对象，字段由 cardType 对应的渲染模板决定 |
| cardType | string | 是 | 卡片类型标识，对应管理端配置的渲染模板名称（如 stock_overview、trade_confirm 等） |
| title | string | 否 | 卡片标题，显示在卡片顶部 |

**选择条件**:
- 需要展示结构化的业务数据，且管理端已配置对应的 cardType 渲染模板
- 数据不适合用图表（折线图/柱状图/饼图）展示，也不需要用户交互选择或确认
- 适合展示信息汇总、概览、摘要等只读内容

**注意事项**:
- 使用前建议先调用 `get_card_config` 工具了解该 cardType 需要的 schema 字段结构
- 如果 cardType 在管理端没有对应模板，前端将使用默认样式渲染
- schema 中的字段名需与渲染模板中定义的字段名一致

---

### 2. render_selectable_list - 可选列表渲染工具

**适用场景**: 需要用户从一组条目中选择一项或多项

**入参格式**:
```json
{
    "title": "请选择要撤销的订单",
    "items": [
        {"orderId": "ORD001", "stock": "阿里巴巴", "qty": 100, "price": 85.2},
        {"orderId": "ORD002", "stock": "腾讯控股", "qty": 200, "price": 320.5}
    ],
    "allowMultiSelect": false,
    "displayFields": ["orderId", "stock", "qty", "price"],
    "fieldLabels": {
        "orderId": "订单号",
        "stock": "股票",
        "qty": "数量",
        "price": "价格"
    }
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| title | string | 是 | 列表标题，引导用户操作 |
| items | array | 是 | 列表条目数组，每个元素为 key-value 对象 |
| allowMultiSelect | boolean | 否 | 是否允许多选，默认 false（单选） |
| displayFields | array | 否 | 指定展示的字段列表，默认展示所有字段 |
| fieldLabels | object | 否 | 字段名到中文标签的映射 |

**选择条件**:
- 用户需要从持仓、订单、历史记录等列表中选择特定条目
- 后续操作依赖用户的选择结果

---

### 3. render_confirm_action - 操作确认渲染工具

**适用场景**: 需要用户确认高风险或不可逆操作

**入参格式**:
```json
{
    "title": "确认买入操作",
    "description": "您即将执行以下股票买入操作，请仔细核对后确认。",
    "details": [
        {"label": "股票代码", "value": "BABA"},
        {"label": "股票名称", "value": "阿里巴巴"},
        {"label": "买入数量", "value": "100股"},
        {"label": "参考价格", "value": "85.20元"},
        {"label": "预计金额", "value": "8520.00元"}
    ],
    "confirmText": "确认买入",
    "cancelText": "取消",
    "riskLevel": "high"
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| title | string | 是 | 操作标题 |
| description | string | 否 | 操作描述，提示用户核对内容 |
| details | array | 是 | 操作详情列表，每项含 label 和 value |
| confirmText | string | 否 | 确认按钮文案，默认"确认" |
| cancelText | string | 否 | 取消按钮文案，默认"取消" |
| riskLevel | string | 否 | 风险等级：low/medium/high，默认 medium；high 时按钮显示红色 |

**选择条件**:
- 用户要执行买入、卖出、撤单等不可逆的金融操作
- 需要用户二次确认以防止误操作

---

## 工具选择决策流程

```
用户发起卡片/交互请求
       ↓
分析交互类型
       ↓
┌──────────────────────────────────────────────┐
│  需要展示结构化业务数据（只读概览/摘要）？        │
│  （管理端有对应 cardType 模板）                 │
│  → 调用 render_generic_card                  │
├──────────────────────────────────────────────┤
│  需要用户选择列表中的某一项？                     │
│  （持仓、订单、历史记录等）                       │
│  → 调用 render_selectable_list               │
├──────────────────────────────────────────────┤
│  需要用户确认某个操作？                          │
│  （买入、卖出、撤单、转账等）                     │
│  → 调用 render_confirm_action                │
└──────────────────────────────────────────────┘
```

---

## 使用示例

### 示例 1: 股票概览卡片（通用卡片）

**用户输入**: "展示阿里巴巴的股票概览信息"

**调用工具**: `render_generic_card`

**工具参数**:
```json
{
    "schema": {
        "stockName": "阿里巴巴(BABA)",
        "currentPrice": 85.20,
        "changePct": "+1.5%",
        "volume": "1.2亿",
        "turnoverRate": "0.85%"
    },
    "cardType": "stock_overview",
    "title": "阿里巴巴股票概览"
}
```

---

### 示例 2: 撤单选择（可选列表）

**用户输入**: "帮我撤销今天的委托单"

**调用工具**: `render_selectable_list`

**工具参数**:
```json
{
    "title": "请选择要撤销的委托单",
    "items": [
        {"orderId": "20240403001", "stock": "阿里巴巴(BABA)", "direction": "买入", "qty": 100, "price": "85.20"},
        {"orderId": "20240403002", "stock": "腾讯控股(0700)", "direction": "卖出", "qty": 500, "price": "320.00"}
    ],
    "allowMultiSelect": false,
    "displayFields": ["orderId", "stock", "direction", "qty", "price"],
    "fieldLabels": {
        "orderId": "委托编号",
        "stock": "股票",
        "direction": "方向",
        "qty": "数量(股)",
        "price": "委托价(元)"
    }
}
```

---

### 示例 3: 买入确认（操作确认）

**用户输入**: "买入100股阿里巴巴"

**调用工具**: `render_confirm_action`

**工具参数**:
```json
{
    "title": "确认股票买入",
    "description": "您即将执行股票买入操作，该操作不可撤销，请仔细核对后确认。",
    "details": [
        {"label": "股票代码", "value": "BABA"},
        {"label": "股票名称", "value": "阿里巴巴"},
        {"label": "操作方向", "value": "买入"},
        {"label": "买入数量", "value": "100股"},
        {"label": "参考价格", "value": "85.20元"},
        {"label": "预计金额", "value": "8520.00元"}
    ],
    "confirmText": "确认买入",
    "cancelText": "取消",
    "riskLevel": "high"
}
```

---

## 注意事项

1. **必须调用工具**: 渲染卡片时必须调用对应工具，不要直接输出 JSON 或 Markdown 表格
2. **schema 字段匹配**: generic_card 的 schema 字段名需与 cardType 对应的渲染模板一致，建议先调用 `get_card_config` 查看字段定义
3. **details 字段完整**: confirm_action 的 details 应包含所有关键信息，让用户能直接判断
4. **riskLevel 设置合理**:
   - `low`: 无风险查询操作
   - `medium`: 一般操作，如调整偏好设置
   - `high`: 金融交易操作（买入、卖出、撤单、转账）
5. **fieldLabels 友好化**: selectable_list 的字段名应配置中文 label，方便用户阅读
6. **cardType 正确**: generic_card 的 cardType 必须是管理端已配置的类型标识，否则前端无法正确渲染模板
