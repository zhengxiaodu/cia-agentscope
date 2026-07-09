"""编排服务：多智能体意图识别 + 编排执行的主流程。

每次 /chat 请求时动态加载配置到内存（YAML + mng 外部意图），
请求结束后内存自动释放。

串起：查询改写 → 意图识别 → 编排器选择 → 编排执行（含守护意图）
对外暴露 run() 异步生成器，yield SSE 事件字符串（与现有 chat_service 兼容）。

重构要点（需求 2+4）：
- create() 只持有 LLM 客户端、prompt 模板等不可变资源
- run() 每次请求时动态加载 YAML 配置 + 获取外部意图 + 权限过滤
- 构建临时的 AgentRegistry / IntentRecognizer / QueryRewriter
- 请求结束时局部变量出作用域，内存自动释放
"""
import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import yaml
from fastapi import Request
from openai import AsyncOpenAI

from agentscope.state import AgentState
from agentscope.event import AgentEvent, ReplyStartEvent
from agentscope.message import AssistantMsg, UserMsg
from agentscope.tool import Bash, Read, Write, Edit, Glob, Grep

from app.config import (
    AGENT_CONFIG_PATH,
    INTENT_CONFIG_PATH,
    SKILL_CONFIG_PATH,
    EXTERNAL_SKILLS_DIR,
    JWT_EXPIRE_HOURS,
)
from app.agents.base import AgentDefinition
from app.agents.factory import AgentFactory
from app.agents.registry import (
    AgentRegistry,
    load_agent_definitions,
)
from app.services.workspace_manager import DockerWorkspaceManager
from app.intent.models import Intent, IntentConfig, IntentResult
from app.intent.rewriter import QueryRewriter
from app.intent.recognizer import IntentRecognizer, load_intent_config
from app.intent.llm_client import create_async_client
from app.orchestrator.parallel import ParallelOrchestrator
from app.orchestrator.pipeline import PipelineOrchestrator
from app.orchestrator.react import ReActOrchestrator
from app.services.chat_service import create_model_from_config
from app.services.mng_service import fetch_external_intents, merge_external_into_memory

logger = logging.getLogger(__name__)

# Redis key：用户融合后的配置（登录时写入，会话时读取）
_REDIS_KEY_USER_CONFIG = "user_config:{user_id}"
_USER_CONFIG_TTL = JWT_EXPIRE_HOURS * 3600  # 与 user_permissions 同 TTL

# 联网搜索技能名（与 skill_config.yml / agent_config.yml 中的 name 一致）
_SEARCH_SKILL_NAME = "bocha_search"


class OrchestratorService:
    """编排服务：持有不可变资源，每次 run() 动态构建请求级组件。

    生命周期由 app.main lifespan 管理，单例存于 app.state。
    """

    def __init__(
        self,
        model_config: dict,
        prompts: dict,
        orchestrator_params: dict,
        intent_client: AsyncOpenAI,
        intent_model_cfg: dict,
        think_prompt: str,
        workspace_manager: DockerWorkspaceManager,
    ):
        self._model_config = model_config
        self._prompts = prompts
        self._orchestrator_params = orchestrator_params
        self._intent_client = intent_client
        self._intent_model_cfg = intent_model_cfg
        self._think_prompt = think_prompt
        self._workspace_manager = workspace_manager

        # 最近一次编排结果引用（供外部提取 agent states）
        self._last_orchestrator: Optional[Any] = None

    @classmethod
    async def create(
        cls,
        model_config: dict,
        workspace_manager: DockerWorkspaceManager,
    ) -> "OrchestratorService":
        """工厂方法：从配置创建编排服务（仅持有不可变资源）。

        不再在启动时加载智能体/skill/意图配置；
        改为在每次 run() 中动态加载（支持外部意图合并）。
        """
        default_model_cfg = model_config.get("models", {}).get("default", {})
        intent_model_cfg = model_config.get("models", {}).get(
            "intent_recognizer", default_model_cfg
        )
        intent_client = create_async_client(intent_model_cfg)
        prompts = model_config.get("prompts", {})

        # 编排器参数仍从 intent_config.yml 读取一次（这些属于系统级配置，不变）
        raw_intent_config = load_intent_config(INTENT_CONFIG_PATH)
        orchestrator_cfg = raw_intent_config.get("orchestrator", {})
        orchestrator_params = {
            "parallel_timeout": orchestrator_cfg.get("parallel_timeout", 60),
            "pipeline_step_timeout": orchestrator_cfg.get("pipeline_step_timeout", 60),
            "react_max_steps": orchestrator_cfg.get("react_max_steps", 8),
        }

        think_prompt = prompts.get("react_think", "")

        return cls(
            model_config=model_config,
            prompts=prompts,
            orchestrator_params=orchestrator_params,
            intent_client=intent_client,
            intent_model_cfg=intent_model_cfg,
            think_prompt=think_prompt,
            workspace_manager=workspace_manager,
        )

    def _create_model_fn(self):
        """创建模型实例的工厂函数（每次调用返回新实例）。"""
        default_model_cfg = self._model_config.get("models", {}).get("default", {})
        return create_model_from_config(default_model_cfg)

    def _create_orchestrator(self, mode: str, agent_factory: AgentFactory):
        """根据模式创建编排器实例（每次请求独立创建，不缓存）。"""
        if mode == "pipeline":
            return PipelineOrchestrator(
                agent_factory=agent_factory,
                step_timeout=self._orchestrator_params["pipeline_step_timeout"],
            )
        elif mode == "react":
            return ReActOrchestrator(
                agent_factory=agent_factory,
                think_client=self._intent_client,
                think_model_config=self._intent_model_cfg,
                think_prompt=self._think_prompt,
                max_steps=self._orchestrator_params["react_max_steps"],
            )
        else:  # parallel (default)
            return ParallelOrchestrator(
                agent_factory=agent_factory,
                timeout=self._orchestrator_params["parallel_timeout"],
            )

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

    async def _fuse_user_config(
        self, access_token: str, permissions: dict
    ) -> dict:
        """步骤 1-4：加载 YAML + 请求 mng 外部意图 + 权限过滤 + 合并。

        返回可 JSON 序列化的 dict，供登录时写入 Redis。
        mng 请求失败仅记日志，降级为只用基础配置。
        """
        # ---- 1. 加载基础配置到内存 ----
        base_agent_defs = load_agent_definitions(AGENT_CONFIG_PATH)
        base_intents_raw = load_intent_config(INTENT_CONFIG_PATH)

        with open(SKILL_CONFIG_PATH, "r", encoding="utf-8") as f:
            base_skill_config = yaml.safe_load(f)
        base_skills = base_skill_config.get("skills", [])

        # ---- 2-3. 请求 mng 获取外部意图 ----
        external_intents = []
        if access_token:
            try:
                external_intents = await fetch_external_intents(access_token)
            except Exception:
                logger.exception(
                    "[OrchestratorService] 登录时获取外部意图失败，仅用基础配置"
                )
                external_intents = []

        # ---- 4. 权限过滤 + 合并配置 ----
        merged_intents, merged_agents, merged_skills = merge_external_into_memory(
            base_intents=base_intents_raw.get("intents", []),
            base_agents=[a.model_dump() for a in base_agent_defs],
            base_skills=base_skills,
            external_intents=external_intents,
            permissions=permissions or {},
            external_skills_dir=EXTERNAL_SKILLS_DIR,
        )
        return {
            "merged_intents": merged_intents,
            "merged_agents": merged_agents,
            "merged_skills": merged_skills,
            "default_orchestration": base_intents_raw.get("default_orchestration", {}),
        }

    async def build_and_cache_user_config(
        self,
        user_id: str,
        access_token: str,
        permissions: dict,
        redis_client,
    ) -> None:
        """登录时融合（YAML + mng 外部意图 + 权限过滤）并写入 Redis。

        供 /chat 会话时直接读取。失败不阻断登录：mng 不可用或 Redis
        写失败均仅记日志，会话时读取不到缓存则走 base-only 兜底。
        """
        fused = await self._fuse_user_config(access_token, permissions)
        if redis_client is not None and user_id:
            try:
                key = _REDIS_KEY_USER_CONFIG.format(user_id=user_id)
                await redis_client.set(
                    key,
                    json.dumps(fused, ensure_ascii=False).encode("utf-8"),
                    ex=_USER_CONFIG_TTL,
                )
                logger.info(f"[OrchestratorService] 用户配置已缓存: {key}")
            except Exception:
                logger.exception(
                    f"[OrchestratorService] 缓存用户配置失败 user={user_id}"
                )

    async def _load_cached_user_config(
        self, user_id: str, redis_client
    ) -> Optional[dict]:
        """会话时从 Redis 读取登录时缓存的融合配置。不存在或失败返回 None。"""
        if not user_id or redis_client is None:
            return None
        try:
            key = _REDIS_KEY_USER_CONFIG.format(user_id=user_id)
            raw = await redis_client.get(key)
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
        except Exception:
            logger.exception(
                f"[OrchestratorService] 读取用户配置缓存失败 user={user_id}"
            )
            return None

    async def _build_request_components(
        self,
        user_id: str,
        redis_client,
        session_id: Optional[str] = None,
        search_enabled: bool = True,
    ) -> tuple:
        """会话时构建临时组件：读 Redis 缓存（步骤 1-4 的产物）+ 步骤 5-8。

        缓存命中 → 直接用登录时融合好的 merged_intents/agents/skills；
        缓存未命中 → base-only 兜底融合（不请求 mng，无外部意图），
        外部意图在下次登录后恢复。会话路径永不发起 mng HTTP 调用。

        步骤：
        5. 通过 DockerWorkspaceManager 获取/创建工作区
        6. 构建 AgentRegistry / AgentFactory
        7. 构建临时识别器 IntentRecognizer
        8. 构建临时改写器 QueryRewriter

        Returns:
            (registry, agent_factory, rewriter, recognizer)
        """
        # ---- 读取登录时缓存的融合配置（步骤 1-4 产物） ----
        fused = await self._load_cached_user_config(user_id, redis_client)
        if fused is not None:
            merged_intents = fused.get("merged_intents", [])
            merged_agents = fused.get("merged_agents", [])
            merged_skills = fused.get("merged_skills", [])
            default_orchestration = fused.get("default_orchestration", {})
        else:
            # 缓存未命中兜底：base-only 融合（不请求 mng，无外部意图）
            logger.warning(
                f"[OrchestratorService] 用户配置缓存未命中 user={user_id}，"
                f"走 base-only 兜底（无外部意图），下次登录后恢复"
            )
            fused = await self._fuse_user_config(access_token="", permissions={})
            merged_intents = fused["merged_intents"]
            merged_agents = fused["merged_agents"]
            merged_skills = fused["merged_skills"]
            default_orchestration = fused["default_orchestration"]

        # ---- 5. 通过 DockerWorkspaceManager 获取/创建工作区 ----
        all_skill_dirs = [s["directory"] for s in merged_skills]
        user_id_safe = user_id or "anonymous"
        session_id_safe = session_id or f"ephemeral-{user_id_safe}"
        workspace = await self._workspace_manager.get_workspace(user_id_safe, session_id_safe)
        if workspace is None:
            workspace = await self._workspace_manager.create_workspace(
                user_id=user_id_safe,
                session_id=session_id_safe,
                skill_dirs=all_skill_dirs,
            )
            
        from tools.chart_tools import render_bar_chart,render_line_chart,render_pie_chart,render_generic_card,render_metric_card,render_confirm_action,render_indicator_table,render_selectable_list
        from agentscope.tool import FunctionTool
        all_tools = [Bash(), Read(), Write(), Edit(), Glob(), Grep(),FunctionTool(render_pie_chart),FunctionTool(render_bar_chart),
                     FunctionTool(render_line_chart),FunctionTool(render_generic_card),FunctionTool(render_metric_card),
                     FunctionTool(render_confirm_action),FunctionTool(render_indicator_table),FunctionTool(render_selectable_list)]
        
        all_skills_meta = await workspace.list_skills()

        # 按请求开关显隐联网搜索技能（workspace 始终装载全部技能，此处按轮次过滤）
        if not search_enabled:
            all_skills_meta = [
                m for m in all_skills_meta
                if (getattr(m, "name", None) or
                    (m.get("name") if isinstance(m, dict) else None)
                    ) != _SEARCH_SKILL_NAME
            ]

        # ---- 6. 构建临时注册表 ----
        agent_defs = [AgentDefinition(**a) for a in merged_agents]
        registry = AgentRegistry(
            definitions=agent_defs,
            workspace=workspace,
            all_tools=all_tools,
            all_skills_meta=all_skills_meta,
            create_model_fn=self._create_model_fn,
        )
        agent_factory = AgentFactory(registry)

        # ---- 7. 构建临时识别器 ----
        intent_configs = [IntentConfig(**item) for item in merged_intents]

        recognizer = IntentRecognizer(
            client=self._intent_client,
            model_config=self._intent_model_cfg,
            recognition_prompt=self._prompts.get("intent_recognition", ""),
            intent_configs=intent_configs,
            default_orchestration=default_orchestration,
        )

        # ---- 8. 构建临时改写器 ----
        rewriter = QueryRewriter(
            client=self._intent_client,
            model_config=self._intent_model_cfg,
            rewrite_prompt=self._prompts.get("rewrite", ""),
        )

        return registry, agent_factory, rewriter, recognizer

    async def run(
        self,
        messages: List[Dict[str, Any]],
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        session_service: Optional[Any] = None,
        agent_id: Optional[str] = None,
        request: Optional[Request] = None,
        search_enabled: bool = True,
    ) -> AsyncGenerator[str, None]:
        """编排主流程：改写 → 识别 → 选择编排器 → 执行。

        每次调用动态构建请求级组件（配置从 YAML + mng 加载到内存），
        调用结束后局部变量出作用域，内存自动释放。

        若传入 agent_id，则跳过改写/识别/编排，直接执行指定智能体。

        Args:
            messages: 前端传入的消息列表（含历史 + 当前用户输入）
            session_id: 会话 id
            user_id: 用户 id（用于加载/保存 AgentState 和权限查询）
            session_service: 会话服务
            agent_id: 可选，指定后走单智能体直接问答
            request: FastAPI Request 对象（用于访问 app.state.redis_client）

        Yields:
            SSE 事件字符串（"data: {...}\n\n" 格式）
        """
        user_input = self._extract_last_user_message(messages)
        history = self._extract_history(messages)

        if not user_input:
            yield self._event({"type": "error", "message": "未检测到有效用户输入"})
            return

        # ===== 动态构建请求级组件（配置加载到内存 + 外部意图合并） =====
        redis_client = None
        if request is not None:
            redis_client = getattr(request.app.state, "redis_client", None)

        registry, agent_factory, rewriter, recognizer = (
            await self._build_request_components(
                user_id=user_id,
                redis_client=redis_client,
                session_id=session_id,
                search_enabled=search_enabled,
            )
        )

        # ========== 单智能体直接问答路径（跳过改写→识别→编排） ==========
        if agent_id:
            yield self._event({
                "type": "orchestration_start",
                "mode": "direct",
                "agent_id": agent_id,
            })

            # 校验 agent_id 是否存在（从内存中的 registry 查找）
            definition = registry.get_definition(agent_id)
            if not definition:
                yield self._event({
                    "type": "error",
                    "message": f"agent_id '{agent_id}' 不存在",
                })
                return

            intent = Intent(id=f"direct_{agent_id}", query=user_input, agent=agent_id)

            # 加载已有 AgentState
            agent_state = None
            if session_service and session_id and user_id:
                try:
                    state_dict = await session_service.load_agent_state(
                        session_id, agent_id
                    )
                    if state_dict:
                        agent_state = AgentState.model_validate(state_dict)
                except Exception:
                    logger.debug(
                        f"[OrchestratorService] 无法加载 {agent_id} 状态，将新建"
                    )

            # 创建 agent 实例
            agent = agent_factory.create_for_agent(
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
                        apply = AssistantMsg(
                            name=event.name, content=[], id=event.reply_id
                        )

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
                logger.exception(
                    f"[OrchestratorService] 单智能体 {agent_id} 执行异常"
                )
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
            rewritten = await rewriter.rewrite(user_input, history)
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
            intent_result = await recognizer.recognize(rewritten, history)
        except Exception:
            logger.exception(
                "[OrchestratorService] 意图识别失败，降级为 general_chat"
            )
            intent_result = IntentResult(
                rewritten_query=rewritten,
                intents=[
                    Intent(id="general_chat", query=rewritten, agent="general_agent")
                ],
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
        mode = recognizer.get_orchestration_mode(intent_result)
        orchestrator = self._create_orchestrator(mode, agent_factory)

        # ④ 加载已有 AgentState（按 agent_id 逐个加载）
        agent_states: Dict[str, AgentState] = {}
        if session_service and session_id and user_id:
            for intent in intent_result.intents:
                aid = intent.agent or "general_agent"
                try:
                    state_dict = await session_service.load_agent_state(
                        session_id, aid
                    )
                    if state_dict:
                        agent_states[aid] = AgentState.model_validate(state_dict)
                except Exception:
                    logger.debug(
                        f"[OrchestratorService] 无法加载 {aid} 状态，将新建"
                    )

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
