"""
卡片配置查询工具

对应 Java 版本:
  - GetCardConfigTool
  - GetCustomComponentConfigTool

提供查询 mng 通用卡片配置和个性化组件配置的功能。
"""
import json
import logging
import os
from typing import Any, Dict, List

import httpx

from tools.tool_constants import GET_CARD_CONFIG, GET_CUSTOM_COMPONENT_CONFIG

logger = logging.getLogger(__name__)

# 从环境变量获取 mng 服务地址，默认为 localhost:8083
MNG_BASE_URL = os.getenv("MNG_BASE_URL", "http://localhost:8083")

# Redis 缓存 key（与 Java 版本保持一致）
KEY_CARDS_LIST = "mng:cards:"
KEY_CARD_SINGLE = "mng:card:"


def _extract_input(raw_input: Any) -> Dict[str, Any]:
    """
    统一处理输入参数:
    - 如果是 list, 取第一个元素
    - 如果是 str, 尝试 JSON 解析
    - 否则直接返回 dict
    """
    if isinstance(raw_input, list):
        if not raw_input:
            return {}
        return raw_input[0] if isinstance(raw_input[0], dict) else {}
    if isinstance(raw_input, str):
        try:
            return json.loads(raw_input)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw_input, dict):
        return raw_input
    return {}


def _build_result(data: Dict[str, Any]) -> str:
    """将结果对象序列化为 JSON 字符串"""
    return json.dumps(data, ensure_ascii=False)


# =====================================================================
#  通用卡片配置查询工具
# =====================================================================

def get_card_config() -> str:
    """
    查询通用卡片组件的配置信息。

    返回所有可用卡片类型列表（含 cardType、cardName、schemaFields），
    让大模型自行判断选择哪种图表；

    注意：renderTemplate 渲染代码由前端页面预加载缓存，AI 只需关注 schemaFields 了解数据格式，无需传递 renderTemplate。

    Returns:
        str: 卡片配置 JSON 字符串
    """
    logger.info(f"[{GET_CARD_CONFIG}] 收到查询请求")

    try:
        logger.info(f"[{GET_CARD_CONFIG}] 查询所有可用卡片列表")
        return _fetch_all_cards()

    except Exception as e:
        logger.error(f"[{GET_CARD_CONFIG}] 处理失败: {e}")
        return _build_result({"error": f"查询失败: {str(e)}"})


def _fetch_all_cards() -> str:
    """获取所有可用卡片列表"""
    try:
        url = f"{MNG_BASE_URL}/api/ui/presentation/cards"

        with httpx.Client(timeout=10.0) as client:
            response = client.get(url)
            logger.info(f"[{GET_CARD_CONFIG}] mng响应: status={response.status_code}")

            if response.status_code != 200:
                return _build_result({
                    "error": f"查询失败，HTTP状态码: {response.status_code}"
                })

            return response.text

    except Exception as e:
        logger.error(f"[{GET_CARD_CONFIG}] 请求mng接口失败: {e}")
        return _build_result({"error": f"查询失败: {str(e)}"})


def _fetch_single_card(card_type: str) -> str:
    """获取单个卡片配置"""
    try:
        url = f"{MNG_BASE_URL}/api/ui/presentation/cards/{card_type}"

        with httpx.Client(timeout=10.0) as client:
            response = client.get(url)
            logger.info(f"[{GET_CARD_CONFIG}] mng响应: status={response.status_code}")

            if response.status_code != 200:
                return _build_result({
                    "error": f"查询失败，HTTP状态码: {response.status_code}"
                })

            resp_data = response.json()
            if not resp_data.get("success") or resp_data.get("data") is None:
                return _build_result({
                    "error": f"卡片配置不存在，cardType={card_type}"
                })

            data = resp_data.get("data", {})
            # 只返回关键字段（与 Java 版本保持一致）
            result = {
                "cardType": data.get("cardType"),
                "cardName": data.get("cardName"),
                "cardDesc": data.get("cardDesc"),
                "schemaFields": data.get("schemaFields"),
                "renderTemplate": data.get("renderTemplate"),
                "triggerRule": data.get("triggerRule"),
            }
            return _build_result(result)

    except Exception as e:
        logger.error(f"[{GET_CARD_CONFIG}] 请求mng接口失败: {e}")
        return _build_result({"error": f"查询失败: {str(e)}"})


# =====================================================================
#  个性化组件配置查询工具
# =====================================================================

@tool
def get_custom_component_config(raw_input: Any) -> str:
    """
    查询 mng 个性化组件配置工具（返回 renderTemplate + configJson）

    参数 (input dict):
        componentType (str, optional): 组件类型标识

    Returns:
        str: 组件配置 JSON 字符串
    """
    logger.info(f"[{GET_CUSTOM_COMPONENT_CONFIG}] 收到查询请求")

    try:
        inp = _extract_input(raw_input)
        component_type = inp.get("componentType") or inp.get("component_type")

        logger.info(f"[{GET_CUSTOM_COMPONENT_CONFIG}] 查询组件配置: componentType={component_type}")

        url = f"{MNG_BASE_URL}/api/ui/presentation/custom-components"

        with httpx.Client(timeout=10.0) as client:
            response = client.get(url)
            logger.info(f"[{GET_CUSTOM_COMPONENT_CONFIG}] mng响应: status={response.status_code}")

            if response.status_code != 200:
                return _build_result({
                    "error": f"查询失败，HTTP状态码: {response.status_code}"
                })

            resp_data = response.json()

            # 如果指定了 componentType，过滤返回对应组件
            if component_type and resp_data.get("success") and resp_data.get("data"):
                data_list = resp_data.get("data", [])
                matched = None
                for item in data_list:
                    if item.get("componentType") == component_type or item.get("type") == component_type:
                        matched = item
                        break

                if matched:
                    return _build_result({
                        "success": True,
                        "data": matched
                    })
                else:
                    return _build_result({
                        "error": f"组件配置不存在，componentType={component_type}"
                    })

            return response.text

    except Exception as e:
        logger.error(f"[{GET_CUSTOM_COMPONENT_CONFIG}] 处理失败: {e}")
        return _build_result({"error": f"查询失败: {str(e)}"})


# =====================================================================
#  工具注册表
# =====================================================================

CARD_CONFIG_TOOL_REGISTRY: Dict[str, callable] = {
    GET_CARD_CONFIG: get_card_config,
    GET_CUSTOM_COMPONENT_CONFIG: get_custom_component_config,
}


# =====================================================================
#  工具定义 (agentscope ServiceToolkit 格式)
# =====================================================================

def get_card_config_tool_definitions() -> List[Dict[str, Any]]:
    """
    返回卡片配置查询工具的定义列表，
    可用于注册到 agentscope 的 ServiceToolkit 或 ToolService
    """
    return [
        {
            "name": GET_CARD_CONFIG,
            "description": (
                "查询通用卡片组件的配置信息。"
                "不传 cardType 时返回所有可用卡片类型列表（含 cardType、cardName、schemaFields），让大模型自行判断选择哪种图表；"
                "传入 cardType 时返回该类型卡片的 schemaFields（数据字段格式说明）。"
                "注意：renderTemplate 渲染代码由前端页面预加载缓存，AI 只需关注 schemaFields 了解数据格式，无需传递 renderTemplate。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cardType": {
                        "type": "string",
                        "description": "卡片类型（可选）。不传时返回所有可用卡片列表；传入具体值时返回该卡片的完整配置。可选值：bar（柱状图）、line（折线图）、pie（饼图）、table（技术指标表格）、metric（指标卡）"
                    },
                },
                "required": [],
            },
        },
        {
            "name": GET_CUSTOM_COMPONENT_CONFIG,
            "description": "查询 mng 个性化组件配置。返回 renderTemplate 和 configJson 等配置信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "componentType": {
                        "type": "string",
                        "description": "组件类型标识（可选）。不传时返回所有可用组件列表。"
                    },
                },
                "required": [],
            },
        },
    ]
