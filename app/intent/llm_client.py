"""LLM 客户端辅助模块：提供非流式的 LLM 调用，用于意图识别、查询改写、ReAct 推理。

与业务智能体的流式 OpenAIChatModel 分离：意图识别等需要完整 JSON 输出，
用 openai SDK 的 AsyncOpenAI 做一次性同步请求更稳妥。
"""
import json
import logging
import re
from typing import Any, Dict, Optional

from openai import AsyncOpenAI

from app.services.langfuse_service import get_current_langfuse

logger = logging.getLogger(__name__)


def create_async_client(model_config: dict) -> AsyncOpenAI:
    """根据 model_config 段创建 AsyncOpenAI 客户端。

    Args:
        model_config: models.xxx 配置段，含 base_url / api_key
    """
    base_url = model_config.get("base_url", "https://api.deepseek.com/v1")
    api_key = model_config.get("api_key", "")
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


async def chat_complete(
    client: AsyncOpenAI,
    model_config: dict,
    system_prompt: str,
    user_prompt: str,
    stage: str = "llm-call",
) -> str:
    """非流式补全，返回完整文本。

    内部创建 Langfuse generation observation，携带真实 token usage。
    observation 通过 OTel 上下文自动挂在当前活跃 span 之下，调用方无需传 langfuse_service。

    Args:
        client: AsyncOpenAI 客户端
        model_config: models.xxx 配置段
        system_prompt: 系统提示
        user_prompt: 用户输入
        stage: 埋点名称（对应 TraceName 的 LLM_* 成员）

    Returns:
        模型输出的完整文本
    """
    model_name = model_config.get("model_name", "deepseek-chat")
    parameters = model_config.get("parameters", {})

    lf = get_current_langfuse()
    obs = None
    if lf is not None and getattr(lf, "enabled", False):
        obs = lf.start_observation(
            name=stage, as_type="generation", model=model_name,
            input=[{"role": "system", "content": system_prompt},
                   {"role": "user", "content": user_prompt}],
        )
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=parameters.get("temperature", 0.1),
            max_tokens=parameters.get("max_tokens", 1024),
            extra_body={"chat_template_kwargs": {"enable_thinking": False}}
        )
        text = response.choices[0].message.content or ""
        if obs is not None and lf is not None:
            usage = getattr(response, "usage", None)
            lf.end_observation(
                obs, output=text,
                usage_details={
                    "input": getattr(usage, "prompt_tokens", 0),
                    "output": getattr(usage, "completion_tokens", 0),
                } if usage else None,
            )
        return text
    except Exception as e:
        if obs is not None and lf is not None:
            lf.end_observation(obs, output={"error": str(e)[:500]},
                               level="ERROR", status_message=type(e).__name__)
        raise


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从可能含 markdown 代码块或解释文字的文本中提取首个 JSON 对象。

    用于兜底处理 LLM 未严格遵守"仅输出 JSON"的情况。
    """
    # 先尝试整体解析
    text = text.strip()
    # 去除 markdown 代码块标记
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 花括号匹配提取首个对象
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None
