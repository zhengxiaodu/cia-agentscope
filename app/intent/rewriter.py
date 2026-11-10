"""查询改写器：把口语化输入结合上下文改写为语义完整的规范查询。"""
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from openai import AsyncOpenAI

from app.intent.llm_client import chat_complete, create_async_client

logger = logging.getLogger(__name__)

# 东八区（Asia/Shanghai）
_SHANGHAI_TZ = timezone(timedelta(hours=8))

# 时间代词正则：覆盖常见中文时间指代词
_TIME_KEYWORDS_PATTERN = re.compile(
    r"今天|今日|明天|明日|后天|大后天|昨天|昨日|前天|大前天|"
    r"本周|这周|上周|下周|本周内|"
    r"本月|这个月|上个月|下个月|近几个月|最近几个月|"
    r"本季度|上季度|下季度|近几个季度|"
    r"今年|本年|去年|上一年|明年|下一年|近几年|"
    r"近几天|最近几天|近几周|最近几周|近几天内|"
    r"最近|近期|此刻|现在|当前|刚刚|刚才"
)


def _get_current_date_str() -> str:
    """获取 Asia/Shanghai 当前时间字符串，格式：2026-07-13 星期一 14:30。"""
    now = datetime.now(_SHANGHAI_TZ)
    weekday_cn = "星期" + "一二三四五六日"[now.weekday()]
    return now.strftime(f"%Y-%m-%d {weekday_cn} %H:%M")


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

    async def rewrite(self, user_input: str, history: Optional[List[dict]] = None) -> Tuple[str, dict]:
        """改写用户输入。

        Args:
            user_input: 用户原始输入
            history: 历史对话上下文 [{role, content}, ...]，可选

        Returns:
            (改写后的查询, {"system_prompt": ..., "user_prompt": ...})；
            无需改写或失败时返回 (原始输入, {})。
        """
        # 无上下文时，仅在含时间代词时才改写（避免每条首条消息都调 LLM）
        if not history:
            if not _TIME_KEYWORDS_PATTERN.search(user_input):
                return user_input, {}

        # 拼接最近若干轮上下文摘要
        recent = history[-6:] if history else []  # 最近 3 轮（user+assistant）
        context_str = "\n".join(
            [f"{m.get('role', 'user')}: {m.get('content', '')}" for m in recent]
        )

        user_prompt = (
            f"【历史对话上下文】\n{context_str}\n\n"
            f"【用户输入】\n{user_input}\n\n"
            f"请输出改写后的查询："
        )

        try:
            # 注入服务器当前时间到 system prompt
            system_prompt = self._rewrite_prompt.replace(
                "{{current_date}}", _get_current_date_str()
            )
            rewritten = await chat_complete(
                self._client,
                self._model_config,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                stage="llm-query-rewrite",
            )
            rewritten = rewritten.strip()
            # 改写为空则降级
            if rewritten:
                logger.info(f"[QueryRewriter] 原始: {user_input} → 改写: {rewritten}")
                return rewritten, {"system_prompt": system_prompt, "user_prompt": user_prompt}
        except Exception:
            logger.exception("[QueryRewriter] 改写失败，使用原始输入")

        return user_input, {}
