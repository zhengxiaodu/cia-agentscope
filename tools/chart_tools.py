"""
图表 & 卡片渲染本地工具

对应 Java 版本:
  - RenderLineChartTool / RenderBarChartTool / RenderPieChartTool
  - RenderIndicatorTableTool / RenderMetricCardTool / RenderVolumeChartTool
  - RenderGenericCardTool
  - RenderSelectableListTool / RenderConfirmActionTool

工具返回标准化 component JSON, 由 AgUiStreamingOutputHandler 拦截后
发送 CUSTOM_COMPONENT 事件到前端渲染。
"""
import json
import logging
from typing import Any, Dict, List, Optional

from agentscope.tool import ToolResponse
from agentscope.message import TextBlock

from tools.tool_constants import (
    RENDER_LINE_CHART,
    RENDER_BAR_CHART,
    RENDER_PIE_CHART,
    RENDER_INDICATOR_TABLE,
    RENDER_METRIC_CARD,
    RENDER_GENERIC_CARD,
    RENDER_SELECTABLE_LIST,
    RENDER_CONFIRM_ACTION,
)

logger = logging.getLogger(__name__)

# 工具元数据，用于自动发现
TOOL_METADATA = {
    "name": "chart_tools",
    "description": "图表和卡片渲染工具集，包含折线图、柱状图、饼图、卡片等渲染功能"
}


# ========== 辅助函数 ==========

def _extract_input(raw_input: Any, kwargs: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    统一处理输入参数:
    - 如果 raw_input 为 None 但 kwargs 不为空，使用 kwargs
    - 如果是 list, 取第一个元素
    - 如果是 str, 尝试 JSON 解析
    - 否则直接返回 dict
    """
    # 优先使用 kwargs（AgentScope 展开的关键字参数）
    if kwargs:
        return kwargs
    
    if isinstance(raw_input, list):
        if not raw_input:
            raise ValueError("输入参数为空数组")
        return raw_input[0] if isinstance(raw_input[0], dict) else {}
    if isinstance(raw_input, str):
        return json.loads(raw_input)
    if isinstance(raw_input, dict):
        return raw_input
    return {}


def _build_result(component: Dict[str, Any]) -> str:
    """将 component 对象序列化为 JSON 字符串"""
    return json.dumps(component, ensure_ascii=False)


# =====================================================================
#  图表渲染工具
# =====================================================================

def render_line_chart(raw_input: Any = None, **kwargs) -> ToolResponse:
    """
    折线图渲染工具 - 用于展示时间序列数据、趋势变化

    参数 (input dict):
        title (str, required): 图表标题
        data (list, required): 数据数组, 每项含 x/y 对应字段
        xField (str, required): X轴字段名
        yField (str, required): Y轴字段名
        yLabel (str, optional): Y轴标签说明

    Returns:
        ToolResponse: 包含标准化 component JSON

    重要: 必须先调用 MCP 工具获取数据，再调用此工具渲染图表！
    """
    logger.info(f"[{RENDER_LINE_CHART}] 收到渲染请求")
    try:
        inp = _extract_input(raw_input, kwargs)
        
        # 调试日志：打印接收到的完整输入
        logger.info(f"[{RENDER_LINE_CHART}] 原始输入: {json.dumps(inp, ensure_ascii=False, default=str)[:500]}")

        # 获取数据（不再强制校验，允许空数据）
        data = inp.get("data")
        if not data or not isinstance(data, list) or len(data) == 0:
            logger.warning(f"[{RENDER_LINE_CHART}] 数据为空，将渲染空图表")
            data = []
        
        # 调试日志：检查数据内容
        logger.info(f"[{RENDER_LINE_CHART}] 数据条数: {len(data)}")
        if len(data) > 0:
            logger.info(f"[{RENDER_LINE_CHART}] 第一条数据: {json.dumps(data[0], ensure_ascii=False)}")
            logger.info(f"[{RENDER_LINE_CHART}] 最后一条数据: {json.dumps(data[-1], ensure_ascii=False)}")
            # 检查 yField 对应的值
            y_field = inp.get("yField", "value")
            y_values = [item.get(y_field) for item in data if y_field in item]
            logger.info(f"[{RENDER_LINE_CHART}] yField '{y_field}' 的所有值: {y_values[:5]}... (共{len(y_values)}个)")

        component = {
            "type": "chart",
            "chartType": "line",
            "title": inp.get("title"),
            "data": data,
            "xField": inp.get("xField"),
            "yField": inp.get("yField"),
        }
        if "yLabel" in inp:
            component["yLabel"] = inp["yLabel"]
        return ToolResponse(content=[TextBlock(type="text", text=_build_result(component))])
    except Exception as e:
        logger.error(f"[{RENDER_LINE_CHART}] 处理失败: {e}")
        return ToolResponse(content=[TextBlock(type="text", text=json.dumps({"error": str(e)}))])


def render_bar_chart(raw_input: Any = None, **kwargs) -> ToolResponse:
    """
    柱状图渲染工具 - 用于分类数据对比

    参数 (input dict):
        title (str, required): 图表标题
        data (list, required): 数据数组
        xField (str, required): X轴字段名 (类别)
        yField (str, required): Y轴字段名 (数值)
        yLabel (str, optional): Y轴标签说明

    重要: 必须先调用 MCP 工具获取数据，再调用此工具渲染图表！
    """
    logger.info(f"[{RENDER_BAR_CHART}] 收到渲染请求")
    try:
        inp = _extract_input(raw_input, kwargs)

        # 获取数据（不再强制校验，允许空数据）
        data = inp.get("data")
        if not data or not isinstance(data, list) or len(data) == 0:
            logger.warning(f"[{RENDER_BAR_CHART}] 数据为空，将渲染空图表")
            data = []

        component = {
            "type": "chart",
            "chartType": "bar",
            "title": inp.get("title"),
            "data": data,
            "xField": inp.get("xField"),
            "yField": inp.get("yField"),
        }
        if "yLabel" in inp:
            component["yLabel"] = inp["yLabel"]
        return ToolResponse(content=[TextBlock(type="text", text=_build_result(component))])
    except Exception as e:
        logger.error(f"[{RENDER_BAR_CHART}] 处理失败: {e}")
        return ToolResponse(content=[TextBlock(type="text", text=json.dumps({"error": str(e)}))])


def render_pie_chart(raw_input: Any = None, **kwargs) -> ToolResponse:
    """
    饼图渲染工具 - 用于展示占比分布

    参数 (input dict):
        title (str, required): 图表标题
        data (list, required): 数据数组, 每项含 name 和 value

    重要: 必须先调用 MCP 工具获取数据，再调用此工具渲染图表！
    """
    logger.info(f"[{RENDER_PIE_CHART}] 收到渲染请求")
    try:
        inp = _extract_input(raw_input, kwargs)

        # 获取数据（不再强制校验，允许空数据）
        data = inp.get("data")
        if not data or not isinstance(data, list) or len(data) == 0:
            logger.warning(f"[{RENDER_PIE_CHART}] 数据为空，将渲染空图表")
            data = []

        component = {
            "type": "chart",
            "chartType": "pie",
            "title": inp.get("title"),
            "data": data,
            "xField": "name",
            "yField": "value",
        }
        return ToolResponse(content=[TextBlock(type="text", text=_build_result(component))])
    except Exception as e:
        logger.error(f"[{RENDER_PIE_CHART}] 处理失败: {e}")
        return ToolResponse(content=[TextBlock(type="text", text=json.dumps({"error": str(e)}))])


def render_indicator_table(raw_input: Any = None, **kwargs) -> ToolResponse:
    """
    指标表格渲染工具 - 用于展示技术指标数据

    参数 (input dict):
        indicators (list, required): 指标数据数组
        title (str, optional): 卡片标题
        stock_name (str, optional): 股票名称
        overall_signal (str, optional): 整体信号

    重要: 必须先调用 MCP 工具获取数据，再调用此工具渲染图表！
    """
    logger.info(f"[{RENDER_INDICATOR_TABLE}] 收到渲染请求")
    try:
        inp = _extract_input(raw_input, kwargs)

        # 获取数据（不再强制校验，允许空数据）
        indicators = inp.get("indicators")
        if not indicators or not isinstance(indicators, list) or len(indicators) == 0:
            logger.warning(f"[{RENDER_INDICATOR_TABLE}] 指标数据为空，将渲染空表格")
            indicators = []

        component = {
            "type": "chart",
            "chartType": "table",
            "indicators": indicators,
        }
        for key in ("title", "stock_name", "overall_signal"):
            if key in inp:
                component[key] = inp[key]
        return ToolResponse(content=[TextBlock(type="text", text=_build_result(component))])
    except Exception as e:
        logger.error(f"[{RENDER_INDICATOR_TABLE}] 处理失败: {e}")
        return ToolResponse(content=[TextBlock(type="text", text=json.dumps({"error": str(e)}))])


def render_metric_card(raw_input: Any = None, **kwargs) -> ToolResponse:
    """
    指标卡渲染工具 - 用于展示股票核心指标

    参数 (input dict):
        stock_name (str, required): 股票名称
        current_price (number, required): 当前股价
        change_pct (number, required): 涨跌幅
        stock_code, pe_ratio, market_cap, turnover_rate,
        support_level, resistance_level, rating (optional)
    """
    logger.info(f"[{RENDER_METRIC_CARD}] 收到渲染请求")
    try:
        inp = _extract_input(raw_input, kwargs)

        # 获取数据（不再强制校验，允许空数据）
        stock_name = inp.get("stock_name")
        current_price = inp.get("current_price")
        if not stock_name or current_price is None:
            logger.warning(f"[{RENDER_METRIC_CARD}] 缺少 stock_name 或 current_price，将渲染空卡片")

        component = {
            "type": "chart",
            "chartType": "metric",
            "stock_name": stock_name,
            "current_price": current_price,
        }
        for key in ("stock_code", "change_pct", "pe_ratio", "market_cap", "turnover_rate",
                     "support_level", "resistance_level", "rating"):
            if key in inp:
                component[key] = inp[key]
        return ToolResponse(content=[TextBlock(type="text", text=_build_result(component))])
    except Exception as e:
        logger.error(f"[{RENDER_METRIC_CARD}] 处理失败: {e}")
        return ToolResponse(content=[TextBlock(type="text", text=json.dumps({"error": str(e)}))])


# =====================================================================
#  通用卡片渲染工具
# =====================================================================

def render_generic_card(raw_input: Any = None, **kwargs) -> ToolResponse:
    """
    通用卡片渲染工具 - 接收 cardType + schema, 前端根据 cardType 取缓存模板渲染

    参数 (input dict):
        schema (dict, required): 业务数据
        cardType (str, required): 卡片类型标识
        title (str, optional): 卡片标题
    """
    logger.info(f"[{RENDER_GENERIC_CARD}] 收到渲染请求")
    try:
        inp = _extract_input(raw_input, kwargs)
        schema_obj = inp.get("schema")
        if schema_obj is None:
            logger.warning(f"[{RENDER_GENERIC_CARD}] schema 为空，将渲染空卡片")
            schema_obj = {}

        card_type = inp.get("cardType", "generic")
        title = inp.get("title", "")

        component = {
            "type": "chart",
            "chartType": card_type,
            "schema": schema_obj,
        }
        if title:
            component["title"] = title

        return ToolResponse(content=[TextBlock(type="text", text=_build_result(component))])
    except Exception as e:
        logger.error(f"[{RENDER_GENERIC_CARD}] 处理失败: {e}")
        return ToolResponse(content=[TextBlock(type="text", text=json.dumps({"error": str(e)}))])


# =====================================================================
#  个性化卡片渲染工具
# =====================================================================

def render_selectable_list(raw_input: Any = None, **kwargs) -> ToolResponse:
    """
    可选列表渲染工具 - 展示可交互列表供用户选择

    参数 (input dict):
        title (str, required): 列表标题
        items (list, required): 列表数据
        fieldLabels (dict, required): 字段名到中文标签映射
        allowMultiSelect (bool, optional): 是否多选, 默认 false
        displayFields (list, optional): 显示的字段列表
    """
    logger.info(f"[{RENDER_SELECTABLE_LIST}] 收到渲染请求")
    try:
        inp = _extract_input(raw_input, kwargs)
        component = {
            "type": "selectable_list",
            "title": inp.get("title"),
            "sample_data": inp.get("items"),
            "allow_multi_select": inp.get("allowMultiSelect", False),
        }
        if "displayFields" in inp:
            component["display_fields"] = inp["displayFields"]
        if "fieldLabels" in inp:
            component["labelMap"] = inp["fieldLabels"]
        return ToolResponse(content=[TextBlock(type="text", text=_build_result(component))])
    except Exception as e:
        logger.error(f"[{RENDER_SELECTABLE_LIST}] 处理失败: {e}")
        return ToolResponse(content=[TextBlock(type="text", text=json.dumps({"error": str(e)}))])


def render_confirm_action(raw_input: Any = None, **kwargs) -> ToolResponse:
    """
    确认操作渲染工具 - 展示需用户二次确认的操作卡片

    参数 (input dict):
        title (str, required): 操作标题
        details (list, required): 操作明细 [{label, value}]
        confirmMessage (str, required): 确认后发送给 AI 的消息
        description (str, optional): 操作说明
        confirmText (str, optional): 确认按钮文字, 默认 '确认'
        cancelText (str, optional): 取消按钮文字, 默认 '取消'
        riskLevel (str, optional): 风险等级 low/medium/high
    """
    logger.info(f"[{RENDER_CONFIRM_ACTION}] 收到渲染请求")
    try:
        inp = _extract_input(raw_input, kwargs)
        component = {
            "type": "confirm_action",
            "title": inp.get("title"),
            "description": inp.get("description", ""),
            "details": inp.get("details", []),
            "confirmText": inp.get("confirmText", "确认"),
            "cancelText": inp.get("cancelText", "取消"),
            "riskLevel": inp.get("riskLevel", "medium"),
            "confirmMessage": inp.get("confirmMessage", "确认"),
        }
        return ToolResponse(content=[TextBlock(type="text", text=_build_result(component))])
    except Exception as e:
        logger.error(f"[{RENDER_CONFIRM_ACTION}] 处理失败: {e}")
        return ToolResponse(content=[TextBlock(type="text", text=json.dumps({"error": str(e)}))])


# =====================================================================
#  工具注册表 - 工具名 -> 处理函数的映射
# =====================================================================

CHART_TOOL_REGISTRY: Dict[str, callable] = {
    RENDER_LINE_CHART: render_line_chart,
    RENDER_BAR_CHART: render_bar_chart,
    RENDER_PIE_CHART: render_pie_chart,
    RENDER_INDICATOR_TABLE: render_indicator_table,
    RENDER_METRIC_CARD: render_metric_card,
    RENDER_GENERIC_CARD: render_generic_card,
    RENDER_SELECTABLE_LIST: render_selectable_list,
    RENDER_CONFIRM_ACTION: render_confirm_action,
}


# =====================================================================
#  工具定义 (agentscope ServiceToolkit 格式)
# =====================================================================

def get_chart_tool_definitions() -> List[Dict[str, Any]]:
    """
    返回所有图表/卡片渲染工具的定义列表,
    可用于注册到 agentscope 的 ServiceToolkit 或 ToolService
    """
    return [
        {
            "name": RENDER_LINE_CHART,
            "description": "渲染折线图，用于展示数据随时间或类别的变化趋势。适用于股价走势、销售趋势等场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "图表标题"},
                    "data": {"type": "array", "description": "图表数据数组", "items": {"type": "object"}},
                    "xField": {"type": "string", "description": "X轴字段名"},
                    "yField": {"type": "string", "description": "Y轴字段名"},
                    "yLabel": {"type": "string", "description": "Y轴标签说明"},
                },
                "required": ["title", "data", "xField", "yField"],
            },
        },
        {
            "name": RENDER_BAR_CHART,
            "description": "渲染柱状图，用于对比不同类别的数据。适用于业绩对比、排名展示等场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "图表标题"},
                    "data": {"type": "array", "description": "图表数据数组", "items": {"type": "object"}},
                    "xField": {"type": "string", "description": "X轴字段名"},
                    "yField": {"type": "string", "description": "Y轴字段名"},
                    "yLabel": {"type": "string", "description": "Y轴标签说明"},
                },
                "required": ["title", "data", "xField", "yField"],
            },
        },
        {
            "name": RENDER_PIE_CHART,
            "description": "渲染饼图，用于展示数据的占比分布。适用于收入构成、市场份额等场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "图表标题"},
                    "data": {
                        "type": "array",
                        "description": "饼图数据数组，每项含 name 和 value",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "number"},
                            },
                            "required": ["name", "value"],
                        },
                    },
                },
                "required": ["title", "data"],
            },
        },
        {
            "name": RENDER_INDICATOR_TABLE,
            "description": "渲染技术指标表格卡片，适用于展示股票技术指标（MACD/KDJ/RSI等）等表格类场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "卡片标题"},
                    "stock_name": {"type": "string", "description": "股票名称"},
                    "indicators": {"type": "array", "description": "指标数据数组", "items": {"type": "object"}},
                    "overall_signal": {"type": "string", "description": "整体信号标签"},
                },
                "required": ["indicators"],
            },
        },
        {
            "name": RENDER_METRIC_CARD,
            "description": "渲染股票核心指标卡片，展示当前价、涨跌幅、市盈率、总市值等关键指标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "stock_name": {"type": "string", "description": "股票名称"},
                    "stock_code": {"type": "string", "description": "股票代码"},
                    "current_price": {"type": "number", "description": "当前股价"},
                    "change_pct": {"type": "number", "description": "涨跌幅百分比"},
                    "pe_ratio": {"type": "number", "description": "市盈率"},
                    "market_cap": {"type": "string", "description": "总市值"},
                    "turnover_rate": {"type": "number", "description": "换手率"},
                    "support_level": {"type": "number", "description": "支撑位价格"},
                    "resistance_level": {"type": "number", "description": "压力位价格"},
                    "rating": {"type": "string", "description": "评级"},
                },
                "required": ["stock_name", "current_price", "change_pct"],
            },
        },
        {
            "name": RENDER_GENERIC_CARD,
            "description": "通用卡片渲染工具。将业务数据 schema 和卡片类型 cardType 发送给前端渲染。使用前请先调用 get_card_config 了解 schema 字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "schema": {"type": "object", "description": "卡片业务数据 JSON 对象"},
                    "cardType": {"type": "string", "description": "卡片类型标识"},
                    "title": {"type": "string", "description": "卡片标题（可选）"},
                },
                "required": ["schema", "cardType"],
            },
        },
        {
            "name": RENDER_SELECTABLE_LIST,
            "description": "渲染可选列表卡片，展示一组可交互条目供用户选择。适用于交易记录选择、持仓选择等场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "列表标题"},
                    "items": {"type": "array", "description": "列表数据数组", "items": {"type": "object"}},
                    "allowMultiSelect": {"type": "boolean", "description": "是否允许多选"},
                    "displayFields": {"type": "array", "description": "显示字段列表", "items": {"type": "string"}},
                    "fieldLabels": {"type": "object", "description": "字段名到中文标签的映射"},
                },
                "required": ["title", "items", "fieldLabels"],
            },
        },
        {
            "name": RENDER_CONFIRM_ACTION,
            "description": "渲染确认操作卡片，向用户展示操作详情并请求二次确认。适用于买入、卖出等高风险操作场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "操作标题"},
                    "description": {"type": "string", "description": "操作说明文字"},
                    "details": {
                        "type": "array",
                        "description": "操作明细列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["label", "value"],
                        },
                    },
                    "confirmText": {"type": "string", "description": "确认按钮文字"},
                    "cancelText": {"type": "string", "description": "取消按钮文字"},
                    "riskLevel": {"type": "string", "description": "风险等级: low/medium/high"},
                    "confirmMessage": {"type": "string", "description": "确认后发送给 AI 的消息"},
                },
                "required": ["title", "details", "confirmMessage"],
            },
        },
    ]
