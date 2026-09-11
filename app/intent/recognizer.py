"""意图识别器：两次 LLM 调用——先识别 intents 列表，再决策关系与执行顺序。"""
import json
import logging
from typing import Dict, List, Optional, Tuple

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
    """意图识别引擎（两次 LLM 调用）。

    第一次：recognize_intents() —— 根据【历史上下文 + 用户输入】识别 intents 列表
    第二次：plan_orchestration() —— 基于 intents 列表决策 relation + execution_order

    降级策略：任一步失败 → 返回 single general_chat + independent，保证可用性。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model_config: dict,
        recognition_prompt: str,
        orchestration_prompt: str,
        intent_configs: List[IntentConfig],
        default_orchestration: dict,
    ):
        """
        Args:
            client: AsyncOpenAI 客户端
            model_config: models.intent_recognizer 配置段
            recognition_prompt: 意图识别 prompt 模板（含 {{intents}} 占位符）
            orchestration_prompt: 意图编排 prompt 模板（含 {{intents_list}} 占位符）
            intent_configs: 全部显式意图配置列表
            default_orchestration: default_orchestration 配置段
        """
        self._client = client
        self._model_config = model_config
        self._recognition_prompt = recognition_prompt
        self._orchestration_prompt = orchestration_prompt
        self._intent_configs = intent_configs
        self._default_orchestration = default_orchestration

        # 构建 id → IntentConfig
        self._intent_map: Dict[str, IntentConfig] = {ic.id: ic for ic in intent_configs}

        # 构建意图树：{一级id: {"config": IntentConfig, "children": [IntentConfig, ...]}}
        # 两遍扫描：第一遍收集所有一级意图（level != 2 或 parent_code 为空，含基础意图 level=None）
        self._tree: Dict[str, dict] = {}
        for ic in intent_configs:
            if ic.level != 2 or not ic.parent_code:
                self._tree[ic.id] = {"config": ic, "children": []}
        # 第二遍：挂载二级意图到父节点；父不存在则作为一级兜底
        for ic in intent_configs:
            if ic.level == 2 and ic.parent_code:
                parent = self._tree.get(ic.parent_code)
                if parent:
                    parent["children"].append(ic)
                else:
                    # 父意图缺失，兜底作为一级
                    self._tree[ic.id] = {"config": ic, "children": []}

        # 预渲染树形意图清单（供 LLM 分层判断）
        lines = []
        for parent_id, node in self._tree.items():
            cfg = node["config"]
            lines.append(f"- 一级意图 {parent_id}: {cfg.name} — {cfg.description}")
            for child in node["children"]:
                lines.append(f"  - 二级意图 {child.id}: {child.name} — {child.description}")
        self._intents_desc = "\n".join(lines)

    # ==================== 第一次 LLM：意图识别 ====================

    async def recognize_intents(
        self,
        user_input: str,
        history: Optional[List[dict]] = None,
    ) -> Tuple[List[Intent], dict]:
        """第一次 LLM 调用：识别 intents 列表（不决策关系）。

        Args:
            user_input: 用户输入（已改写或原始）
            history: 历史对话上下文

        Returns:
            (识别出的意图列表, {"system_prompt": ..., "user_prompt": ...})；
            失败降级为 (单条 general_chat, {})。
        """
        try:
            raw_json, prompts = await self._call_recognition_llm(user_input, history)
            return self._parse_intents(raw_json, user_input), prompts
        except Exception:
            logger.exception("[IntentRecognizer] 意图识别失败，降级为 general_chat")
            return [
                Intent(id="general_chat", query=user_input, agent="general_agent")
            ], {}

    def _build_recognition_prompt(self, user_input: str, history: Optional[List[dict]]) -> str:
        """拼接意图识别 prompt（填 {{intents}} + 历史上下文 + 用户输入）。"""
        prompt = self._recognition_prompt.replace("{{intents}}", self._intents_desc)

        user_msg = f"【用户输入】\n{user_input}"
        if history:
            recent = history[-6:]
            context_str = "\n".join(
                [f"  {m.get('role', 'user')}: {m.get('content', '')}" for m in recent]
            )
            user_msg = f"【历史上下文】\n{context_str}\n\n{user_msg}"

        return prompt + user_msg

    async def _call_recognition_llm(self, user_input: str, history: Optional[List[dict]]) -> Tuple[dict, dict]:
        """调用 LLM 识别 intents，返回解析后的 JSON dict 和提示词。"""
        system_prompt = "你是一个严格输出 JSON 的意图识别引擎，不要输出任何非 JSON 内容。"
        user_prompt = self._build_recognition_prompt(user_input, history)
        raw_text = await chat_complete(
            self._client,
            self._model_config,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            stage="llm-intent-recognition",
        )
        data = extract_json(raw_text)
        if data is None:
            raise ValueError(f"LLM 输出无法解析为 JSON: {raw_text[:200]}")
        return data, {"system_prompt": system_prompt, "user_prompt": user_prompt}

    def _parse_intents(self, data: dict, original_query: str) -> List[Intent]:
        """将 LLM 输出解析为 Intent 列表，回填 agent 映射。"""
        raw_intents = data.get("intents", [])
        intents: List[Intent] = []
        for item in raw_intents:
            intent_id = item.get("id", "general_chat")
            intent_id = self._normalize_intent_id(intent_id)
            intent_config = self._intent_map.get(intent_id)
            intents.append(Intent(
                id=intent_id,
                query=item.get("query", original_query),
                params=item.get("params", {}),
                agent=intent_config.agent if intent_config else "general_agent",
            ))
        if not intents:
            intents.append(Intent(
                id="general_chat", query=original_query, agent="general_agent",
            ))
        return intents

    # ==================== 第二次 LLM：意图编排 ====================

    async def plan_orchestration(
        self,
        user_input: str,
        intents: List[Intent],
    ) -> Tuple[str, List[int], dict]:
        """第二次 LLM 调用：基于已识别 intents 决策 relation + execution_order。

        Args:
            user_input: 用户输入（供 LLM 判断意图依赖关系）
            intents: 第一次识别出的意图列表

        Returns:
            (relation, execution_order, {"system_prompt": ..., "user_prompt": ...})；
            失败降级为 ("independent", [], {})。
        """
        try:
            raw_json, prompts = await self._call_orchestration_llm(user_input, intents)
            return (*self._parse_orchestration(raw_json, len(intents)), prompts)
        except Exception:
            logger.exception("[IntentRecognizer] 意图编排失败，降级为 independent")
            return ("independent", [], {})

    def _build_orchestration_prompt(self, user_input: str, intents: List[Intent]) -> str:
        """拼接意图编排 prompt（填 {{intents_list}} + 用户输入）。"""
        intents_list = "\n".join(
            f"[{i}] id={intent.id}, query={intent.query}"
            for i, intent in enumerate(intents)
        )
        prompt = self._orchestration_prompt.replace("{{intents_list}}", intents_list)
        return prompt + f"\n【用户输入】\n{user_input}"

    async def _call_orchestration_llm(self, user_input: str, intents: List[Intent]) -> Tuple[dict, dict]:
        """调用 LLM 决策编排，返回解析后的 JSON dict 和提示词。"""
        system_prompt = "你是一个严格输出 JSON 的意图编排决策引擎，不要输出任何非 JSON 内容。"
        user_prompt = self._build_orchestration_prompt(user_input, intents)
        raw_text = await chat_complete(
            self._client,
            self._model_config,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            stage="llm-intent-orchestration",
        )
        data = extract_json(raw_text)
        if data is None:
            raise ValueError(f"LLM 输出无法解析为 JSON: {raw_text[:200]}")
        return data, {"system_prompt": system_prompt, "user_prompt": user_prompt}

    def _parse_orchestration(self, data: dict, intents_count: int) -> Tuple[str, List[int]]:
        """解析 relation + execution_order，校验长度一致性。"""
        relation = data.get("relation", "independent")
        if relation not in ("independent", "related_fixed", "related_dynamic"):
            relation = "independent"

        raw_order = data.get("execution_order", [])
        execution_order: List[int] = []
        if isinstance(raw_order, list):
            # 校验：长度需与 intents 一致、索引在范围内、无重复
            if len(raw_order) == intents_count and all(
                isinstance(i, int) and 0 <= i < intents_count for i in raw_order
            ) and len(set(raw_order)) == intents_count:
                execution_order = raw_order
            else:
                logger.warning(
                    f"[IntentRecognizer] execution_order {raw_order} 与 intents 数量 "
                    f"{intents_count} 不符或索引非法，忽略按原顺序执行"
                )
        return (relation, execution_order)

    # ==================== 兼容入口 + 辅助 ====================

    async def recognize(
        self,
        user_input: str,
        history: Optional[List[dict]] = None,
    ) -> IntentResult:
        """原子化识别入口：内部依次调 recognize_intents + plan_orchestration。

        供需要一次性得到完整 IntentResult 的场景使用。
        run() 通常显式调两步以支持独立 span/进度事件。
        """
        intents, _ = await self.recognize_intents(user_input, history)
        relation, execution_order, _ = await self.plan_orchestration(user_input, intents)
        # 按 execution_order 重排 intents（若有效）
        if execution_order:
            intents = [intents[i] for i in execution_order]
        return IntentResult(
            rewritten_query=user_input,
            intents=intents,
            relation=relation,
            execution_order=execution_order,
        )

    def _normalize_intent_id(self, intent_id: str) -> str:
        """规范化意图 id：未知 id 降级为 general_chat。"""
        if intent_id in self._intent_map:
            return intent_id
        logger.warning(f"[IntentRecognizer] 未知意图 id={intent_id}，降级为 general_chat")
        return "general_chat"

    def get_orchestration_mode(self, result: IntentResult) -> str:
        """根据 IntentResult 的 relation 决定编排模式。

        Returns:
            "parallel" | "pipeline" | "react"
        """
        mapping = {
            "independent": self._default_orchestration.get(
                "multi_independent" if result.is_multi_intent else "single_intent",
                "parallel",
            ),
            "related_fixed": self._default_orchestration.get("multi_related_fixed", "pipeline"),
            "related_dynamic": self._default_orchestration.get("multi_related_dynamic", "react"),
        }
        return mapping.get(result.relation, "pipeline")
