"""意图识别器：LLM 单次调用输出结构化 JSON，联系上下文做多意图识别。"""
import json
import logging
from typing import Dict, List, Optional

import yaml
from openai import AsyncOpenAI

from app.intent.llm_client import chat_complete, create_async_client, extract_json
from app.intent.models import Intent, IntentConfig, IntentResult

logger = logging.getLogger(__name__)


def load_intent_config(config_path: str) -> dict:
    """从 intent_config.yml 加载意图配置原始数据。"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class IntentRecognizer:
    """意图识别引擎。

    根据【历史上下文 + 用户输入】，调用 LLM 一次输出结构化 JSON，
    从 intent_config.yml 定义的意图清单中匹配，识别多意图及关系。

    降级策略：识别失败 → 返回 single general_chat 意图，保证可用性。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model_config: dict,
        recognition_prompt: str,
        intent_configs: List[IntentConfig],
        default_orchestration: dict,
    ):
        """
        Args:
            client: AsyncOpenAI 客户端
            model_config: models.intent_recognizer 配置段
            recognition_prompt: 意图识别 prompt 模板（含 {{intents}} 占位符）
            intent_configs: 全部显式意图配置列表
            default_orchestration: default_orchestration 配置段
        """
        self._client = client
        self._model_config = model_config
        self._recognition_prompt = recognition_prompt
        self._intent_configs = intent_configs
        self._default_orchestration = default_orchestration

        # 构建 id → IntentConfig
        self._intent_map: Dict[str, IntentConfig] = {ic.id: ic for ic in intent_configs}

        # 预渲染 prompt 中的意图清单文本（供 LLM 参考）
        self._intents_desc = "\n".join(
            [f"- {ic.id}: {ic.name} — {ic.description}" for ic in intent_configs]
        )

    async def recognize(
        self,
        user_input: str,
        history: Optional[List[dict]] = None,
    ) -> IntentResult:
        """识别意图。

        Args:
            user_input: 用户输入（已改写或原始）
            history: 历史对话上下文

        Returns:
            IntentResult：包含改写查询、意图列表、关系
        """
        try:
            raw_json = await self._call_llm(user_input, history)
            return self._parse_result(raw_json, user_input)
        except Exception:
            logger.exception("[IntentRecognizer] 意图识别失败，降级为 general_chat")
            return self._fallback(user_input)

    def _build_recognition_prompt(self, user_input: str, history: Optional[List[dict]]) -> str:
        """拼接完整的意图识别 prompt。"""
        # 填充模板占位符
        prompt = self._recognition_prompt
        prompt = prompt.replace("{{intents}}", self._intents_desc)

        # 用户消息
        user_msg = f"【用户输入】\n{user_input}"

        # 附加历史上下文
        if history:
            recent = history[-6:]
            context_str = "\n".join(
                [f"  {m.get('role', 'user')}: {m.get('content', '')}" for m in recent]
            )
            user_msg = f"【历史上下文】\n{context_str}\n\n{user_msg}"

        return prompt + user_msg

    async def _call_llm(self, user_input: str, history: Optional[List[dict]]) -> dict:
        """调用 LLM 进行意图识别，返回解析后的 JSON dict。"""
        user_prompt = self._build_recognition_prompt(user_input, history)

        raw_text = await chat_complete(
            self._client,
            self._model_config,
            system_prompt="你是一个严格输出 JSON 的意图识别引擎，不要输出任何非 JSON 内容。",
            user_prompt=user_prompt,
        )

        data = extract_json(raw_text)
        if data is None:
            raise ValueError(f"LLM 输出无法解析为 JSON: {raw_text[:200]}")
        return data

    def _parse_result(self, data: dict, original_query: str) -> IntentResult:
        """将 LLM 输出的 JSON dict 解析为 IntentResult，回填 agent 映射。"""
        # 解析意图列表
        raw_intents = data.get("intents", [])
        intents: List[Intent] = []
        for item in raw_intents:
            intent_id = item.get("id", "general_chat")
            intent_id = self._normalize_intent_id(intent_id)
            intent_config = self._intent_map.get(intent_id)

            intent = Intent(
                id=intent_id,
                query=item.get("query", original_query),
                params=item.get("params", {}),
                agent=intent_config.agent if intent_config else "general_agent",
            )
            intents.append(intent)

        # 解析关系
        relation = data.get("relation", "independent")
        if relation not in ("independent", "related_fixed", "related_dynamic"):
            relation = "independent"

        # 单意图但 LLM 未识别到任何意图 → 兜底
        if not intents:
            intents.append(Intent(
                id="general_chat",
                query=original_query,
                agent="general_agent",
            ))

        return IntentResult(
            rewritten_query=data.get("rewritten_query", original_query),
            intents=intents,
            relation=relation,
        )

    def _normalize_intent_id(self, intent_id: str) -> str:
        """规范化意图 id：未知 id 降级为 general_chat。"""
        if intent_id in self._intent_map:
            return intent_id
        logger.warning(f"[IntentRecognizer] 未知意图 id={intent_id}，降级为 general_chat")
        return "general_chat"

    def _fallback(self, original_query: str) -> IntentResult:
        """降级兜底：返回 single general_chat。"""
        return IntentResult(
            rewritten_query=original_query,
            intents=[
                Intent(id="general_chat", query=original_query, agent="general_agent")
            ],
            relation="independent",
        )

    def get_orchestration_mode(self, result: IntentResult) -> str:
        """根据 IntentResult 的 relation 决定编排模式。

        Returns:
            "parallel" | "pipeline" | "react"
        """
        # relation 直接决定编排模式：
        # - independent: 无关联/单意图简单任务 → 并行
        # - related_fixed: 有关联固定顺序 → 流水线
        # - related_dynamic: 有关联动态决策（含复杂意图多步推理）→ ReAct
        mapping = {
            "independent": self._default_orchestration.get(
                "multi_independent" if result.is_multi_intent else "single_intent",
                "parallel",
            ),
            "related_fixed": self._default_orchestration.get("multi_related_fixed", "pipeline"),
            "related_dynamic": self._default_orchestration.get("multi_related_dynamic", "react"),
        }
        return mapping.get(result.relation, "parallel")
