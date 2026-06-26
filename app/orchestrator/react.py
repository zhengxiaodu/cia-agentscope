"""ReAct 编排器：有关联·动态决策 → Thought→Act→Observe 循环动态编排 + 思维链。

适用场景：
- 任务路径不确定、需根据中间结果动态决策
- 复杂意图需要多步推理（如事件影响分析 → 持仓映射 → 风险识别 → 建议）

设计原则（来自文档）：
- 复杂场景引入 ReAct，让智能体自主规划路径
- 设计思维链模板引导推理，避免跳步或遗漏
- 可提前终止（条件满足即出结论）
- 无限循环兜底（MAX_STEPS 上限）
"""
import asyncio
import json
import logging
from typing import AsyncGenerator, Dict, Optional

from agentscope.state import AgentState

from openai import AsyncOpenAI

from app.agents.factory import AgentFactory
from app.intent.models import IntentResult
from app.orchestrator.base import BaseOrchestrator
from app.intent.llm_client import chat_complete, extract_json

logger = logging.getLogger(__name__)

# ReAct 循环最大步数
MAX_REACT_STEPS = 8


class ReActOrchestrator(BaseOrchestrator):
    """ReAct 动态编排器。

    执行流程：
    1. 初始化推理链记忆 scratch = ""
    2. 循环（最多 MAX_STEPS 次）：
       a. Thought：LLM 根据 scratch 决定下一步（is_final? next_action?）
       b. 若 is_final → 输出结论，结束
       c. Act：执行选定的智能体/动作
       d. Observe：收集结果，追加到 scratch
    3. 步数超限 → 兜底输出
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        think_client: AsyncOpenAI,
        think_model_config: dict,
        think_prompt: str,
        max_steps: int = MAX_REACT_STEPS,
    ):
        """
        Args:
            agent_factory: 智能体工厂
            think_client: ReAct 推理用的 LLM 客户端
            think_model_config: models.intent_recognizer 配置段
            think_prompt: ReAct 推理 prompt 模板（含 {{task}}/{{scratch}}/{{available_actions}}）
            max_steps: 最大推理步数
        """
        super().__init__(agent_factory)
        self._think_client = think_client
        self._think_model_config = think_model_config
        self._think_prompt = think_prompt
        self._max_steps = max_steps

    async def run(
        self,
        intent_result: IntentResult,
        session_id: Optional[str] = None,
        agent_states: Optional[Dict[str, AgentState]] = None,
    ) -> AsyncGenerator[str, None]:
        """执行 ReAct 循环。"""
        yield self._event({
            "type": "orchestration_start",
            "mode": "react",
            "intent_count": len(intent_result.intents),
        })

        # 初始化推理链
        scratch = ""
        task_desc = self._build_task_description(intent_result)
        available_actions = self._build_available_actions(intent_result)
        self._last_results = []

        for step in range(1, self._max_steps + 1):
            yield self._event({
                "type": "react_step",
                "step": step,
                "max_steps": self._max_steps,
            })

            # Thought：LLM 推理下一步
            thought = await self._think(task_desc, scratch, available_actions)

            if thought.get("is_final"):
                conclusion = thought.get("conclusion", "")
                yield self._event({
                    "type": "react_final",
                    "step": step,
                    "thought": thought.get("thought", ""),
                    "conclusion": conclusion,
                })
                yield self._event({
                    "type": "summary",
                    "content": conclusion,
                })
                return

            # Act：执行选定动作
            action = thought.get("next_action")
            if not action:
                logger.warning(f"[ReAct] 第 {step} 步无 action，终止循环")
                yield self._event({
                    "type": "react_final",
                    "step": step,
                    "thought": "无法确定下一步动作",
                    "conclusion": "抱歉，当前任务过于复杂，无法自动完成。请尝试将问题拆分后分别提问。",
                })
                return

            action_name = action.get("name", "")
            action_args = action.get("args", {})

            yield self._event({
                "type": "react_act",
                "step": step,
                "action": action_name,
                "thought": thought.get("thought", ""),
            })

            # 执行智能体
            observation = await self._execute_action(
                action_name, action_args, intent_result, session_id, agent_states
            )

            # Observe：追加到 scratch
            step_record = (
                f"\n--- 步骤 {step} ---\n"
                f"Thought: {thought.get('thought', '')}\n"
                f"Action: {action_name} ({json.dumps(action_args, ensure_ascii=False)})\n"
                f"Observation: {observation}\n"
            )
            scratch += step_record

            yield self._event({
                "type": "react_observe",
                "step": step,
                "observation_preview": observation[:200],
            })

        # 步数超限兜底
        yield self._event({
            "type": "react_timeout",
            "max_steps": self._max_steps,
        })
        yield self._event({
            "type": "summary",
            "content": "推理步数已达上限，请尝试将问题简化或分步骤提问。",
        })

    def _build_task_description(self, intent_result: IntentResult) -> str:
        """构建任务目标描述。"""
        parts = [f"用户需求: {intent_result.rewritten_query}"]
        for intent in intent_result.intents:
            parts.append(f"- 子意图 {intent.id}: {intent.query} (由 {intent.agent} 处理)")
        return "\n".join(parts)

    def _build_available_actions(self, intent_result: IntentResult) -> str:
        """构建可用动作列表描述。"""
        agents = set()
        for intent in intent_result.intents:
            agents.add(intent.agent or "general_agent")

        action_lines = []
        for agent_id in sorted(agents):
            action_lines.append(
                f'- 调用智能体 "{agent_id}" 处理子任务: '
                f'{{"name": "call_agent", "args": {{"agent_id": "{agent_id}", "query": "具体子任务描述"}}}}'
            )
        action_lines.append(
            '- 直接给出最终结论: {"name": "final", "args": {"conclusion": "结论内容"}}'
        )
        return "\n".join(action_lines)

    async def _think(self, task: str, scratch: str, available_actions: str) -> dict:
        """调用 LLM 推理下一步。"""
        prompt = self._think_prompt
        prompt = prompt.replace("{{task}}", task)
        prompt = prompt.replace("{{scratch}}", scratch or "(暂无)")
        prompt = prompt.replace("{{available_actions}}", available_actions)

        raw_text = await chat_complete(
            self._think_client,
            self._think_model_config,
            system_prompt="你是一个 ReAct 推理引擎，严格输出 JSON。",
            user_prompt=prompt,
        )

        data = extract_json(raw_text)
        if data is None:
            logger.error(f"[ReAct] 推理输出解析失败: {raw_text[:200]}")
            return {"is_final": True, "conclusion": "推理过程异常，无法继续。"}

        return data

    async def _execute_action(
        self,
        action_name: str,
        action_args: dict,
        intent_result: IntentResult,
        session_id: Optional[str] = None,
        agent_states: Optional[Dict[str, AgentState]] = None,
    ) -> str:
        """执行 ReAct 中选定的一步动作。"""
        if action_name == "final":
            return action_args.get("conclusion", "")

        if action_name == "call_agent":
            agent_id = action_args.get("agent_id", "general_agent")
            query = action_args.get("query", "")

            from app.intent.models import Intent
            intent = Intent(
                id=f"react_{agent_id}",
                query=query,
                agent=agent_id,
            )

            agent_state = (agent_states or {}).get(agent_id)
            result = await self._run_single_agent(
                intent,
                session_id=session_id,
                agent_state=agent_state,
            )
            if result.final_state:
                self._last_results.append(result)
            return result.output if result.success else f"执行失败: {result.output}"

        return f"未知动作: {action_name}"
