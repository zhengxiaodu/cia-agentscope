---
name: chart_renderer
description: |
  智能图表渲染技能，根据数据特征自动选择最合适的图表类型并通过工具渲染。
  Use when: 需要将数据可视化展示、用户要求图表展示、数据分析结果适合用图表呈现。
license: MIT
metadata:
  source: skill-dir
  tools:
    - render_line_chart
    - render_bar_chart
    - render_pie_chart
    - render_indicator_table
    - render_metric_card
---

# 智能图表渲染 Skill

## 功能描述

本技能根据数据特征智能选择最合适的图表类型，通过调用对应的图表渲染工具，生成前端可视化组件。

## 可用工具

### 1. render_line_chart - 折线图渲染工具

**适用场景**: 时间序列数据、趋势变化展示

**入参格式**:
```json
{
    "title": "图表标题",
    "data": [
        {"date": "2024-03-11", "close": 85.2},
        {"date": "2024-03-12", "close": 87.1}
    ],
    "xField": "date",
    "yField": "close",
    "yLabel": "收盘价（元）"
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| title | string | 是 | 图表标题 |
| data | array | 是 | 数据数组，每个元素包含 x/y 轴对应的字段 |
| xField | string | 是 | X轴字段名，如 date、time、month |
| yField | string | 是 | Y轴字段名，如 value、close、price |
| yLabel | string | 否 | Y轴标签说明 |

**选择条件**:
- 数据具有时间序列特征（日期、月份、年份递进）
- 需要展示趋势变化
- 数据点数量 > 2

---

### 2. render_bar_chart - 柱状图渲染工具

**适用场景**: 分类数据对比、离散数据比较

**入参格式**:
```json
{
    "title": "各业务板块营收对比",
    "data": [
        {"category": "电商", "value": 420},
        {"category": "云计算", "value": 280},
        {"category": "物流", "value": 150}
    ],
    "xField": "category",
    "yField": "value",
    "yLabel": "营收（亿元）"
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| title | string | 是 | 图表标题 |
| data | array | 是 | 数据数组，每个元素包含类别和数值字段 |
| xField | string | 是 | X轴（类别）字段名，如 category、name、product |
| yField | string | 是 | Y轴（数值）字段名，如 value、amount、count |
| yLabel | string | 否 | Y轴标签说明 |

**选择条件**:
- 数据为分类对比
- 类别数量在 2-12 之间
- 需要强调数值差异

---

### 3. render_pie_chart - 饼图渲染工具

**适用场景**: 占比数据、构成比例展示

**入参格式**:
```json
{
    "title": "2024年业务收入构成",
    "data": [
        {"name": "电商业务", "value": 272650},
        {"name": "云计算", "value": 73222},
        {"name": "物流", "value": 69540},
        {"name": "其他", "value": 121568}
    ]
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| title | string | 是 | 图表标题 |
| data | array | 是 | 数据数组，每个元素必须包含 name 和 value |
| data[].name | string | 是 | 类别名称 |
| data[].value | number | 是 | 数值 |

**选择条件**:
- 数据表示部分与整体的关系
- 类别数量在 2-8 之间
- 各部分之和有意义（如占比、构成）

---

### 4. render_indicator_table - 技术指标表格渲染工具

**适用场景**: 展示股票技术指标（MACD/KDJ/RSI等）、基本面对比数据等表格类场景

**入参格式**:
```json
{
    "stock_name": "贵州茅台",
    "overall_signal": "看多",
    "indicators": [
        {"name": "MACD", "value": "+12.35", "signal": "金叉", "strength": "强"},
        {"name": "KDJ", "value": "68.52", "signal": "中性", "strength": "中"},
        {"name": "RSI(14)", "value": "56.78", "signal": "正常", "strength": "中"}
    ]
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| stock_name | string | 否 | 股票名称，显示在标题中 |
| overall_signal | string | 否 | 整体信号，如"看多"、"看空"、"中性" |
| indicators | array | 是 | 指标列表 |
| indicators[].name | string | 是 | 指标名称，如 MACD、KDJ |
| indicators[].value | string | 是 | 当前值 |
| indicators[].signal | string | 否 | 信号，如"金叉"、"中性"、"看多" |
| indicators[].strength | string | 否 | 强度，如"强"、"中"、"弱" |

**选择条件**:
- 需要展示多个指标的对比表格
- 数据有名称、数值、信号等维度

---

### 5. render_metric_card - 核心指标卡渲染工具

**适用场景**: 展示股票核心指标（当前价、涨跌幅、市盈率、市值、支撑压力位等）

**入参格式**:
```json
{
    "stock_name": "贵州茅台",
    "stock_code": "600519.SH",
    "current_price": 1856.50,
    "change_pct": 1.20,
    "pe_ratio": 32.5,
    "market_cap": "2.33万亿",
    "turnover_rate": 8.2,
    "support_level": 1845,
    "resistance_level": 1920,
    "rating": "买入评级"
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| stock_name | string | 是 | 股票名称 |
| current_price | number | 是 | 当前股价 |
| change_pct | number | 是 | 涨跌幅百分比（正数涨，负数跌）|
| pe_ratio | number | 否 | 市盈率(PE) |
| market_cap | string | 否 | 总市值，如"2.33万亿" |
| turnover_rate | number | 否 | 换手率百分比 |
| support_level | number | 否 | 支撑位价格 |
| resistance_level | number | 否 | 压力位价格 |
| rating | string | 否 | 评级，如"买入评级"、"持有" |

**选择条件**:
- 需要突出展示股票当前价格和涨跌情况
- 需要一览关键财务/技术指标

---

## 图表选择决策流程

```
用户请求数据可视化
       ↓
分析数据特征
       ↓
┌──────────────────────────────────────────────┐
│  时间序列？（日期、月份递进）                    │
│  → 调用 render_line_chart                     │
├──────────────────────────────────────────────┤
│  分类对比？（2-12个类别，需要比较数值）          │
│  → 调用 render_bar_chart                      │
├──────────────────────────────────────────────┤
│  占比构成？（部分与整体，2-8个类别）             │
│  → 调用 render_pie_chart                      │
├──────────────────────────────────────────────┤
│  技术指标表格？（MACD/KDJ/RSI等多指标对比）      │
│  → 调用 render_indicator_table               │
├──────────────────────────────────────────────┤
│  核心指标卡？（当前价/涨跌/PE/市值等关键数据）   │
│  → 调用 render_metric_card                   │
└──────────────────────────────────────────────┘
```

---

## 使用示例

### 示例 1: 股价走势（折线图）

**用户输入**: "展示阿里巴巴最近一周的股价走势"

**调用工具**: `render_line_chart`

**工具参数**:
```json
{
    "title": "阿里巴巴(BABA)近一周股价走势",
    "data": [
        {"date": "2024-03-11", "close": 85.2},
        {"date": "2024-03-12", "close": 87.1},
        {"date": "2024-03-13", "close": 86.5},
        {"date": "2024-03-14", "close": 88.3},
        {"date": "2024-03-15", "close": 89.0}
    ],
    "xField": "date",
    "yField": "close",
    "yLabel": "收盘价（美元）"
}
```

---

### 示例 2: 季度营收对比（柱状图）

**用户输入**: "对比各季度的营收数据"

**调用工具**: `render_bar_chart`

**工具参数**:
```json
{
    "title": "2024年各季度营收对比",
    "data": [
        {"quarter": "Q1", "revenue": 2340},
        {"quarter": "Q2", "revenue": 2580},
        {"quarter": "Q3", "revenue": 2420},
        {"quarter": "Q4", "revenue": 2890}
    ],
    "xField": "quarter",
    "yField": "revenue",
    "yLabel": "营收（亿元）"
}
```

---

### 示例 3: 业务收入构成（饼图）

**用户输入**: "展示各业务板块的收入占比"

**调用工具**: `render_pie_chart`

**工具参数**:
```json
{
    "title": "阿里巴巴2024财年业务收入构成",
    "data": [
        {"name": "中国电商", "value": 272650},
        {"name": "国际数字商业", "value": 69540},
        {"name": "云智能", "value": 73222},
        {"name": "菜鸟物流", "value": 56890},
        {"name": "本地生活", "value": 42300},
        {"name": "大文娱", "value": 22378}
    ]
}
```

---

### 示例 4: 技术指标表格

**用户输入**: "展示贵州茅台的技术指标分析"

**调用工具**: `render_indicator_table`

**工具参数**:
```json
{
    "stock_name": "贵州茅台",
    "overall_signal": "看多",
    "indicators": [
        {"name": "MACD", "value": "+12.35", "signal": "金叉", "strength": "强"},
        {"name": "KDJ", "value": "68.52", "signal": "中性", "strength": "中"},
        {"name": "RSI(14)", "value": "56.78", "signal": "正常", "strength": "中"},
        {"name": "MA20", "value": "1845.20", "signal": "支撑", "strength": "强"}
    ]
}
```

---

### 示例 5: 核心指标卡

**用户输入**: "查看贵州茅台今日行情和关键指标"

**调用工具**: `render_metric_card`

**工具参数**:
```json
{
    "stock_name": "贵州茅台",
    "stock_code": "600519.SH",
    "current_price": 1856.50,
    "change_pct": 1.20,
    "pe_ratio": 32.5,
    "market_cap": "2.33万亿",
    "turnover_rate": 8.2,
    "support_level": 1845,
    "resistance_level": 1920,
    "rating": "买入评级"
}
```

---

## 注意事项

1. **必须调用工具**: 渲染图表时必须调用对应的图表工具，不要直接输出 JSON
2. **数据格式正确**: 确保 data 数组中的字段名与 xField/yField 一致
3. **标题简洁**: 标题应包含关键信息和单位
4. **数据量控制**:
   - 折线图: 建议 < 100 个数据点
   - 柱状图: 建议 < 15 个类别
   - 饼图: 建议 < 8 个类别
5. **字段命名**: 使用有意义的英文字段名（如 date、value、name）
