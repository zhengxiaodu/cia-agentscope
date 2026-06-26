"""查询改写器：把口语化输入结合上下文改写为语义完整的规范查询。"""
import logging
from typing import List, Optional

from openai import AsyncOpenAI

from app.intent.llm_client import chat_complete, create_async_client

logger = logging.getLogger(__name__)


class QueryRewriter:
    """查询改写器。

    根据历史上下文把用户口语化、省略、指代的输入改写为语义完整的查询。
    若无历史上下文或改写失败，原样返回（降级）。
    """

    def __init__(self, client: AsyncOpenAI, model_config: dict, rewrite_prompt: str):
        """
        Args:
            client: AsyncOpenAI 客户端（复用 intent_recognizer 的客户端）
            model_config: models.xxx 配置段
            rewrite_prompt: 查询改写 prompt 模板（来自 model_config.prompts.rewrite）
        """
        self._client = client
        self._model_config = model_config
        self._rewrite_prompt = rewrite_prompt

    async def rewrite(self, user_input: str, history: Optional[List[dict]] = None) -> str:
        """改写用户输入。

        Args:
            user_input: 用户原始输入
            history: 历史对话上下文 [{role, content}, ...]，可选

        Returns:
            改写后的查询；无上下文或失败时原样返回
        """
        # 无上下文无需改写
        if not history:
            return user_input

        # 拼接最近若干轮上下文摘要
        recent = history[-6:]  # 最近 3 轮（user+assistant）
        context_str = "\n".join(
            [f"{m.get('role', 'user')}: {m.get('content', '')}" for m in recent]
        )

        user_prompt = (
            f"【历史对话上下文】\n{context_str}\n\n"
            f"【用户输入】\n{user_input}\n\n"
            f"请输出改写后的查询："
        )

        try:
            rewritten = await chat_complete(
                self._client,
                self._model_config,
                system_prompt=self._rewrite_prompt,
                user_prompt=user_prompt,
            )
            rewritten = rewritten.strip()
            # 改写为空则降级
            if rewritten:
                logger.info(f"[QueryRewriter] 原始: {user_input} → 改写: {rewritten}")
                return rewritten
        except Exception:
            logger.exception("[QueryRewriter] 改写失败，使用原始输入")

        return user_input
