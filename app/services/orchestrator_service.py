"""编排服务：多智能体意图识别 + 编排执行的主流程。

串起：查询改写 → 意图识别 → 编排器选择 → 编排执行（含守护意图）
对外暴露 run() 异步生成器，yield SSE 事件字符串（与现有 chat_service 兼容）。
"""
import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import yaml
from openai import AsyncOpenAI

from agentscope.state import AgentState

from app.config import AGENT_CONFIG_PATH, INTENT_CONFIG_PATH, SKILL_CONFIG_PATH
from app.agents.factory import AgentFactory
from app.agents.registry import AgentRegistry, load_agent_definitions, load_all_skills
from app.intent.models import IntentConfig, IntentResult
from app.intent.rewriter import QueryRewriter
from app.intent.recognizer import IntentRecognizer, load_intent_config
from app.intent.llm_client import create_async_client
from app.orchestrator.parallel import ParallelOrchestrator
from app.orchestrator.pipeline import PipelineOrchestrator
from app.orchestrator.react import ReActOrchestrator
from app.services.chat_service import create_model_from_config

from agentscope.event import AgentEvent, ReplyStartEvent
from agentscope.message import AssistantMsg, UserMsg

logger = logging.getLogger(__name__)


class OrchestratorService:
    """编排服务：初始化并持有所有组件，对外暴露 run()。

    生命周期由 app.main lifespan 管理，单例存于 app.state。
    """

    def __init__(
        self,
        registry: AgentRegistry,
        agent_factory: AgentFactory,
        rewriter: QueryRewriter,
        recognizer: IntentRecognizer,
        orchestrator_params: dict,
        think_client: Optional[AsyncOpenAI] = None,
        think_model_config: Optional[dict] = None,
        think_prompt: str = "",
    ):
        self.registry = registry
        self.agent_factory = agent_factory
        self.rewriter = rewriter
        self.recognizer = recognizer
        self._orchestrator_params = orchestrator_params
        self._think_client = think_client
        self._think_model_config = think_model_config or {}
        self._think_prompt = think_prompt

        # 缓存编排器实例
        self._parallel: Optional[ParallelOrchestrator] = None
        self._pipeline: Optional[PipelineOrchestrator] = None
        self._react: Optional[ReActOrchestrator] = None
        self._last_orchestrator: Optional[Any] = None

    @classmethod
    async def create(cls, model_config: dict) -> "OrchestratorService":
        """工厂方法：从配置文件创建完整编排服务。"""
        # ① 加载智能体定义 + skill
        agent_defs = load_agent_definitions(AGENT_CONFIG_PATH)
        workspace, all_tools, all_skills_meta = await load_all_skills(
            skill_config_path=SKILL_CONFIG_PATH,
            workdir="./my-workspace",
        )

        # 创建模型工厂函数
        default_model_cfg = model_config.get("models", {}).get("default", {})
        def _create_model():
            return create_model_from_config(default_model_cfg)

        # ② 创建 AgentRegistry
        registry = AgentRegistry(
            definitions=agent_defs,
            workspace=workspace,
            all_tools=all_tools,
            all_skills_meta=all_skills_meta,
            create_model_fn=_create_model,
        )

        # ③ 创建 AgentFactory
        agent_factory = AgentFactory(registry)

        # ④ 创建意图识别 LLM 客户端
        intent_model_cfg = model_config.get("models", {}).get(
            "intent_recognizer", default_model_cfg
        )
        intent_client = create_async_client(intent_model_cfg)
        prompts = model_config.get("prompts", {})

        # ⑤ 创建 QueryRewriter
        rewriter = QueryRewriter(
            client=intent_client,
            model_config=intent_model_cfg,
            rewrite_prompt=prompts.get("rewrite", ""),
        )

        # ⑥ 创建 IntentRecognizer
        raw_intent_config = load_intent_config(INTENT_CONFIG_PATH)

        intent_configs = [
            IntentConfig(**item) for item in raw_intent_config.get("intents", [])
        ]
        default_orchestration = raw_intent_config.get("default_orchestration", {})

        recognizer = IntentRecognizer(
            client=intent_client,
            model_config=intent_model_cfg,
            recognition_prompt=prompts.get("intent_recognition", ""),
            intent_configs=intent_configs,
            default_orchestration=default_orchestration,
        )

        # ⑧ 编排器参数
        orchestrator_cfg = raw_intent_config.get("orchestrator", {})
        orchestrator_params = {
            "parallel_timeout": orchestrator_cfg.get("parallel_timeout", 60),
            "pipeline_step_timeout": orchestrator_cfg.get("pipeline_step_timeout", 60),
            "react_max_steps": orchestrator_cfg.get("react_max_steps", 8),
        }

        # ⑨ ReAct 推理用的 client + prompt（复用 intent_recognizer）
        think_prompt = prompts.get("react_think", "")

        return cls(
            registry=registry,
            agent_factory=agent_factory,
            rewriter=rewriter,
            recognizer=recognizer,
            orchestrator_params=orchestrator_params,
            think_client=intent_client,
            think_model_config=intent_model_cfg,
            think_prompt=think_prompt,
        )

    def _get_parallel(self) -> ParallelOrchestrator:
        if self._parallel is None:
            self._parallel = ParallelOrchestrator(
                agent_factory=self.agent_factory,
                timeout=self._orchestrator_params["parallel_timeout"],
            )
        return self._parallel

    def _get_pipeline(self) -> PipelineOrchestrator:
        if self._pipeline is None:
            self._pipeline = PipelineOrchestrator(
                agent_factory=self.agent_factory,
                step_timeout=self._orchestrator_params["pipeline_step_timeout"],
            )
        return self._pipeline

    def _get_react(self) -> ReActOrchestrator:
        if self._react is None:
            self._react = ReActOrchestrator(
                agent_factory=self.agent_factory,
                think_client=self._think_client,
                think_model_config=self._think_model_config,
                think_prompt=self._think_prompt,
                max_steps=self._orchestrator_params["react_max_steps"],
            )
        return self._react

    def _select_orchestrator(self, intent_result: IntentResult):
        """根据意图关系选择编排器。"""
        mode = self.recognizer.get_orchestration_mode(intent_result)
        if mode == "pipeline":
            return self._get_pipeline()
        elif mode == "react":
            return self._get_react()
        else:
            return self._get_parallel()

    @property
    def last_agent_states(self) -> Dict[str, dict]:
        """获取最近一次编排中所有 agent 的最终状态 dict。

        Returns:
            {agent_id: state_dict, ...}
        """
        if not self._last_orchestrator:
            return {}
        states = {}
        for r in self._last_orchestrator._last_results:
            if r.final_state:
                states[r.agent_id] = r.final_state
        return states

    @staticmethod
    def _event(data: dict) -> str:
        """序列化 SSE 事件。"""
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    @staticmethod
    def _extract_last_user_message(messages: List[Dict[str, Any]]) -> str:
        """从 messages 列表提取最后一条用户消息文本。"""
        for msg in reversed(messages):
            content = msg.get("content", "")
            if msg.get("role") == "user":
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    return "\n".join(text_parts)
        return ""

    @staticmethod
    def _extract_history(messages: List[Dict[str, Any]]) -> List[dict]:
        """从 messages 提取历史对话上下文（排除最后一条，即当前用户输入）。"""
        history = []
        for msg in messages[:-1]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = "\n".join(text_parts)
            if role in ("user", "assistant") and content:
                history.append({"role": role, "content": content})
        return history

    async def run(
        self,
        messages: List[Dict[str, Any]],
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        session_service: Optional[Any] = None,
        agent_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """编排主流程：改写 → 识别 → 选择编排器 → 执行。

        若传入 agent_id，则跳过改写/识别/编排，直接执行指定智能体。

        Args:
            messages: 前端传入的消息列表（含历史 + 当前用户输入）
            session_id: 会话 id
            user_id: 用户 id（用于加载/保存 AgentState）
            session_service: 会话服务（用于加载/保存 AgentState）
            agent_id: 可选，指定后走单智能体直接问答

        Yields:
            SSE 事件字符串（"data: {...}\n\n" 格式）
        """
        user_input = self._extract_last_user_message(messages)
        history = self._extract_history(messages)

        if not user_input:
            yield self._event({"type": "error", "message": "未检测到有效用户输入"})
            return

        # ========== 单智能体直接问答路径（跳过改写→识别→编排） ==========
        if agent_id:
            yield self._event({
                "type": "orchestration_start",
                "mode": "direct",
                "agent_id": agent_id,
            })

            # 校验 agent_id 是否存在
            definition = self.registry.get_definition(agent_id)
            if not definition:
                yield self._event({
                    "type": "error",
                    "message": f"agent_id '{agent_id}' 不存在",
                })
                return

            from app.intent.models import Intent
            intent = Intent(id=f"direct_{agent_id}", query=user_input, agent=agent_id)

            # 加载已有 AgentState
            agent_state = None
            if session_service and session_id and user_id:
                try:
                    state_dict = await session_service.load_agent_state(session_id, agent_id)
                    if state_dict:
                        agent_state = AgentState.model_validate(state_dict)
                except Exception:
                    logger.debug(f"[OrchestratorService] 无法加载 {agent_id} 状态，将新建")

            # 创建 agent 实例
            agent = self.agent_factory.create_for_agent(
                agent_id=agent_id,
                session_id=session_id,
                agent_state=agent_state,
            )
            if agent is None:
                yield self._event({
                    "type": "error",
                    "message": f"无法创建智能体 '{agent_id}'",
                })
                return

            # 执行单 agent 对话
            user_msg = UserMsg(name="user", content=user_input)
            apply = None
            final_output_parts = []

            try:
                async for event in agent.reply_stream(user_msg):
                    if isinstance(event, ReplyStartEvent):
                        apply = AssistantMsg(name=event.name, content=[], id=event.reply_id)

                    if isinstance(event, AgentEvent):
                        if apply:
                            apply.append_event(event)
                        yield f"data: {event.model_dump_json()}\n\n"

                if apply:
                    text_parts = []
                    for block in apply.content:
                        if hasattr(block, "type") and block.type == "text":
                            text_parts.append(getattr(block, "text", str(block)))
                    final_output = "\n".join(text_parts).strip()
                    final_output_parts.append(final_output)

                # 保存 AgentState
                final_state = agent.state.model_dump()
                if session_service and session_id and user_id and final_state:
                    await session_service.save_agent_state(
                        session_id, user_id, agent_id, final_state,
                    )

            except Exception as e:
                logger.exception(f"[OrchestratorService] 单智能体 {agent_id} 执行异常")
                yield self._event({
                    "type": "error",
                    "message": f"执行出错: {str(e)}",
                })
                return

            # yield summary 事件
            if final_output_parts:
                yield self._event({
                    "type": "summary",
                    "content": final_output_parts[0],
                })

            return  # 跳过后续改写→识别→编排流程

        # ① 查询改写（联系上下文）
        try:
            rewritten = await self.rewriter.rewrite(user_input, history)
        except Exception:
            logger.exception("[OrchestratorService] 查询改写失败，使用原始输入")
            rewritten = user_input

        yield self._event({
            "type": "query_rewritten",
            "original": user_input,
            "rewritten": rewritten,
        })

        # ② 意图识别
        try:
            intent_result = await self.recognizer.recognize(rewritten, history)
        except Exception:
            logger.exception("[OrchestratorService] 意图识别失败，降级为 general_chat")
            from app.intent.models import Intent
            intent_result = IntentResult(
                rewritten_query=rewritten,
                intents=[Intent(id="general_chat", query=rewritten, agent="general_agent")],
                relation="independent",
            )
        yield self._event({
            "type": "intents_recognized",
            "intents": [
                {"id": i.id, "agent": i.agent, "query": i.query}
                for i in intent_result.intents
            ],
            "relation": intent_result.relation,
        })

        # ③ 选择编排器
        orchestrator = self._select_orchestrator(intent_result)

        # ④ 加载已有 AgentState（按 agent_id 逐个加载）
        agent_states: Dict[str, AgentState] = {}
        if session_service and session_id and user_id:
            for intent in intent_result.intents:
                agent_id = intent.agent or "general_agent"
                try:
                    state_dict = await session_service.load_agent_state(session_id, agent_id)
                    if state_dict:
                        agent_states[agent_id] = AgentState.model_validate(state_dict)
                except Exception:
                    logger.debug(f"[OrchestratorService] 无法加载 {agent_id} 状态，将新建")

        # ⑤ 执行编排（内部 yield SSE 事件）
        async for event_str in orchestrator.run(
            intent_result,
            session_id=session_id,
            agent_states=agent_states,
        ):
            yield event_str

        # ⑥ 保存编排结果引用（供外部提取 agent states）
        self._last_orchestrator = orchestrator

        # ⑦ 持久化所有 AgentState
        if session_service and session_id and user_id:
            for r in orchestrator._last_results:
                if r.final_state:
                    try:
                        await session_service.save_agent_state(
                            session_id, user_id, r.agent_id, r.final_state,
                        )
                    except Exception:
                        logger.exception(
                            f"[OrchestratorService] 保存 agent {r.agent_id} 状态失败"
                        )
