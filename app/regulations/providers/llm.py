"""LLM 工厂 — 创建 LangChain ChatOpenAI 实例（httpx 流式，捕获 reasoning_content）。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

from app.regulations.providers.trace import get_serial

logger = logging.getLogger(__name__)


class HttpxChatOpenAI(ChatOpenAI):
    """ChatOpenAI 子类，用 httpx 实现流式，捕获 reasoning_content。"""

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """用 httpx 处理流式请求，将 reasoning_content 写入 additional_kwargs。"""
        payload = {
            "model": self.model_name,
            "messages": [{"role": _langchain_role(m), "content": m.content} for m in messages],
            "temperature": self.temperature,
            "stream": True,
        }
        if self.extra_body:
            payload.update(self.extra_body)

        api_key = self.openai_api_key
        if hasattr(api_key, "get_secret_value"):
            api_key = api_key.get_secret_value()

        client = httpx.AsyncClient(timeout=self.request_timeout or 120)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if serial := get_serial():
            headers["app_serial_number"] = serial
        try:
            request = client.build_request(
                "POST",
                f"{self.openai_api_base}/chat/completions",
                headers=headers,
                json=payload,
            )

            # 对瞬时错误（429 / 5xx）做有限重试：DeepSeek 等上游会间歇性返回 503，
            # 自研 httpx 传输绕过了 OpenAI SDK 的自动重试，这里手动补上。
            # 重试只发生在任何 token 输出之前（raise_for_status 阶段），不会重复内容。
            transient_codes = (429, 500, 502, 503, 504)
            max_retries = 3
            response = None
            for attempt in range(1, max_retries + 1):
                response = await client.send(request, stream=True)
                if response.status_code not in transient_codes:
                    break
                await response.aclose()
                if attempt == max_retries:
                    break
                logger.warning(
                    f"LLM 上游瞬时错误 HTTP {response.status_code}，"
                    f"第 {attempt}/{max_retries} 次重试"
                )
                await asyncio.sleep(min(2 ** attempt, 8))

            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # OpenAI 兼容平台（LM Studio/vLLM 等）流式末尾可能返回 usage 统计块
                # （choices 为空）或偶发空块，跳过避免 IndexError 越界。
                if not isinstance(data, dict) or not data.get("choices"):
                    continue
                choice = data["choices"][0]
                delta = choice.get("delta", {})
                content = delta.get("content", "") or ""
                # 思考内容字段名不统一：中债平台用 delta.reasoning，
                # DeepSeek 等标准 OpenAI 兼容平台用 delta.reasoning_content。
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""

                chunk_msg = AIMessageChunk(
                    content=content,
                    additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
                )
                yield ChatGenerationChunk(message=chunk_msg)
        finally:
            await client.aclose()

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """非流式调用：httpx 直连，注入 app_serial_number，解析 usage。

        用与 _astream 一致的 httpx 路径，使 rewrite / 建议问题等非流式调用
        也能从 context 拿到全链路流水号。
        """
        payload = {
            "model": self.model_name,
            "messages": [{"role": _langchain_role(m), "content": m.content} for m in messages],
            "temperature": self.temperature,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        if self.extra_body:
            payload.update(self.extra_body)

        api_key = self.openai_api_key
        if hasattr(api_key, "get_secret_value"):
            api_key = api_key.get_secret_value()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if serial := get_serial():
            headers["app_serial_number"] = serial

        client = httpx.AsyncClient(timeout=self.request_timeout or 120)
        try:
            response = await client.post(
                f"{self.openai_api_base}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(
                    f"LLM 返回空 choices: {json.dumps(data, ensure_ascii=False)[:300]}"
                )
            choice = choices[0]
            msg = choice.get("message", {})
            content = msg.get("content", "") or ""
            # 非流式思考字段同样兼容 message.reasoning / message.reasoning_content
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            usage = data.get("usage", {})
            ai_msg = AIMessage(
                content=content,
                additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
                usage_metadata={
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            )
            return ChatResult(generations=[ChatGeneration(message=ai_msg, text=content)])
        finally:
            await client.aclose()


def _langchain_role(msg: BaseMessage) -> str:
    if msg.type == "system":
        return "system"
    if msg.type == "ai":
        return "assistant"
    return "user"


def detect_thinking_mode(model: str) -> str | None:
    """根据模型名判断思维链支持类型。"""
    m = model.lower()
    if "deepseek" in m:
        return "deepseek"
    if "qwen" in m:
        return "qwen"
    return None


def resolve_thinking(request_thinking: bool, model: str) -> tuple[bool, str | None]:
    """根据前端 thinking 参数与模型名解析 (thinking_enabled, thinking_mode)。

    无论 thinking 开关如何都要识别模型类型——关闭时也要把 thinking_mode 传给
    build_llm，让它发关闭参数（如 qwen 的 enable_thinking:false）。否则
    thinking=false 时 build_llm 拿不到模型类型、什么都不发，平台 Qwen3.6 默认
    思考，首字 token 延迟漫长。
    """
    thinking_enabled = request_thinking
    thinking_mode = detect_thinking_mode(model)
    if thinking_enabled and not thinking_mode:
        logger.info(f"model {model} 不支持思考模式，忽略 thinking=true")
        thinking_enabled = False
    return thinking_enabled, thinking_mode


def build_llm(
    model_cfg: dict,
    thinking_enabled: bool = False,
    thinking_mode: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> HttpxChatOpenAI:
    """按请求参数构建 HttpxChatOpenAI 实例（httpx 流式，捕获 reasoning_content）。

    model_cfg 为 resolve_regulations_model 的返回值
    （model_name/base_url/api_key/parameters）。

    思考开关必须包进 chat_template_kwargs 一层（内网 LM Studio 要求；顶层
    enable_thinking 会被忽略，导致思考不吐 reasoning_content / 关闭失效）：
      - qwen 系列：{"chat_template_kwargs": {"enable_thinking": true/false}}
      - deepseek 系列：{"chat_template_kwargs": {"thinking": true/false}}
    公网 DeepSeek API 会忽略未知参数，此格式对本地开发无影响。

    temperature / max_tokens 为可选覆盖：改写 LLM 用低温（0.1）与较小 max_tokens。
    temperature 缺省取 parameters.temperature（0.3），timeout 缺省取
    parameters.timeout（120）。
    """
    parameters = model_cfg.get("parameters") or {}

    extra_body: dict[str, Any] = {}

    if thinking_mode == "qwen":
        extra_body["chat_template_kwargs"] = {"enable_thinking": thinking_enabled}
    elif thinking_mode == "deepseek":
        extra_body["chat_template_kwargs"] = {"thinking": thinking_enabled}

    if max_tokens is not None:
        # 自定义 HttpxChatOpenAI 的 payload 只透传 extra_body，不读 self.max_tokens，
        # 故 max_tokens 必须放进 extra_body 才能随请求发出。
        extra_body["max_tokens"] = max_tokens

    return HttpxChatOpenAI(
        model=model_cfg.get("model_name", ""),
        api_key=model_cfg.get("api_key", ""),
        base_url=model_cfg.get("base_url", ""),
        temperature=temperature if temperature is not None else float(parameters.get("temperature", 0.3)),
        timeout=parameters.get("timeout", 120),
        extra_body=extra_body if extra_body else None,
    )


def get_llm(model_cfg: dict) -> HttpxChatOpenAI:
    """构建通用 LLM（标题生成、建议问题等无思考场景）。

    模型配置运行时注入（resolve_regulations_model 的返回值），不做缓存。
    """
    return build_llm(model_cfg)


def get_rewrite_llm(model_cfg: dict) -> HttpxChatOpenAI:
    """构建改写专用 LLM：低温（0.1）、关闭 thinking、限制 max_tokens=1024。

    改写是快速变换，不混用请求 thinking LLM，省 token、省首字延迟，也不污染正文流。
    """
    return build_llm(
        model_cfg,
        thinking_enabled=False,
        thinking_mode=detect_thinking_mode(model_cfg.get("model_name", "")),
        temperature=0.1,
        max_tokens=1024,
    )
