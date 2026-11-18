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
import asyncio
import json
import logging
from contextlib import AsyncExitStack, aclosing, contextmanager
from typing import Any, AsyncGenerator, Dict, Iterator, List, Optional

from fastapi import Request
from openai import AsyncOpenAI

from agentscope.state import AgentState
from agentscope.message import AssistantMsg, TextBlock, UserMsg

from app.config import INTENT_CONFIG_PATH
from app.agents.factory import AgentFactory
from app.agents.registry import AgentRegistry
from app.services.opensandbox_workspace_manager import OpenSandboxWorkspaceManager
from app.services.round_state import RoundState
from app.intent.models import Intent, IntentConfig, IntentResult
from app.intent.rewriter import QueryRewriter
from app.intent.recognizer import IntentRecognizer, load_intent_config
from app.intent.llm_client import create_async_client
from app.orchestrator.parallel import ParallelOrchestrator
from app.orchestrator.pipeline import PipelineOrchestrator
from app.orchestrator.react import ReActOrchestrator
from app.services.chat_service import (
    FILE_ONLY_DEFAULT_QUESTION,
    create_model_from_config,
)
from app.services.agent_state_store import load_agent_state
from app.services.upload_context_provider import (
    append_upload_context,
    build_recognizer_file_hint,
    has_unbound_uploads,
    load_upload_context,
    persist_upload_files_to_workspace,
    wait_for_upload_parsing,
)
from app.services.user_config_service import (
    build_and_cache_user_config as user_config_build_and_cache,
    load_config_bundle,
)
from app.services.workspace_assembler import (
    _SEARCH_SKILL_NAME,
    assemble_workspace_components,
)

logger = logging.getLogger(__name__)


@contextmanager
def _noop_ctx() -> Iterator[None]:
    """空 context manager，langfuse 未启用时作为 start_span 的占位，yield None。"""
    yield None


def _is_context_length_error(exc: Exception) -> bool:
    """判断异常是否为模型上下文超长类错误（400）。

    匹配多种引擎/SDK 的错误文本：OpenAI BadRequestError、私有引擎
    "maximum context length" / "context_length_exceeded" / "token limit" 等。
    命中时可清空历史上下文后重试一次，避免整轮对话直接中断。
    """
    msg = str(exc).lower()
    keywords = (
        "context_length_exceeded",
        "maximum context length",
        "context window",
        "token limit",
        "too many tokens",
        "input too long",
        "request too large",
        "exceeds maximum",          # 私有引擎：exceeds maximum allowed limit
        "input length",             # 私有引擎：Input length (X) exceeds ...
        "error code: 400",          # OpenAI BadRequestError 通用兜底
    )
    return any(k in msg for k in keywords)


def _safe_update_span(span: Any, output_dict: dict) -> None:
    """安全更新 span 的 output 字段，span 为 None 或更新异常均静默忽略。"""
    if not span:
        return
    try:
        span.update(output=output_dict)
    except Exception:
        pass


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
        summary_client: AsyncOpenAI,
        summary_model_cfg: dict,
        think_prompt: str,
        workspace_manager: OpenSandboxWorkspaceManager,
        explicit_only_agents: Optional[List[str]] = None,
        agent_bound_tools: Optional[dict] = None,
        input_probe_client: Optional[AsyncOpenAI] = None,
        input_probe_model_cfg: Optional[dict] = None,
    ):
        self._model_config = model_config
        self._prompts = prompts
        self._orchestrator_params = orchestrator_params
        self._intent_client = intent_client
        self._intent_model_cfg = intent_model_cfg
        self._summary_client = summary_client
        self._summary_model_cfg = summary_model_cfg
        self._think_prompt = think_prompt
        self._workspace_manager = workspace_manager
        # 显式触发智能体集合：这些智能体对应的意图不进入意图识别
        # 候选集（自动路由不可见），仅用户显式选择时走直答路径生效
        self._explicit_only_agents: set = set(explicit_only_agents or [])
        # 工具智能体绑定：声明的工具只注入给指定智能体（工具名 → 智能体 id 列表）
        self._agent_bound_tools: dict = dict(agent_bound_tools or {})
        # 输入围栏 403 预检专用客户端（models.input_guard 配置段，
        # 与 summary/intent 客户端完全独立；None 时预检自动跳过）
        self._input_probe_client = input_probe_client
        self._input_probe_model_cfg = input_probe_model_cfg
        # 注意：本服务是全局单例，不持有任何请求级状态（agent_ids /
        # success / token 计数 / 消费的上传文件 id 均由每轮独立的
        # RoundState 承载，见 round_state.py），避免并发请求互相覆盖

    @classmethod
    async def create(
        cls,
        model_config: dict,
        workspace_manager: OpenSandboxWorkspaceManager,
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
        # 并行编排结果汇总用客户端（默认业务大模型）
        summary_client = create_async_client(default_model_cfg)
        # 输入围栏 403 预检专用客户端（models.input_guard 配置段）：
        # 独立实例、独立配置，不与意图识别/汇总共用；段缺失时回退 default
        # 配置但仍创建新客户端
        input_probe_model_cfg = model_config.get("models", {}).get(
            "input_guard", default_model_cfg
        )
        input_probe_client = create_async_client(input_probe_model_cfg)
        prompts = model_config.get("prompts", {})

        # 编排器参数仍从 intent_config.yml 读取一次（这些属于系统级配置，不变）
        raw_intent_config = load_intent_config(INTENT_CONFIG_PATH)
        orchestrator_cfg = raw_intent_config.get("orchestrator", {})
        orchestrator_params = {
            "parallel_timeout": orchestrator_cfg.get("parallel_timeout", 60),
            "pipeline_step_timeout": orchestrator_cfg.get("pipeline_step_timeout", 60),
            "react_max_steps": orchestrator_cfg.get("react_max_steps", 8),
        }
        # 显式触发智能体（系统级配置，读取一次）
        explicit_only_agents = raw_intent_config.get("explicit_only_agents") or []
        # 工具智能体绑定（系统级配置，读取一次）
        agent_bound_tools = raw_intent_config.get("agent_bound_tools") or {}

        think_prompt = prompts.get("react_think", "")

        return cls(
            model_config=model_config,
            prompts=prompts,
            orchestrator_params=orchestrator_params,
            intent_client=intent_client,
            intent_model_cfg=intent_model_cfg,
            summary_client=summary_client,
            summary_model_cfg=default_model_cfg,
            think_prompt=think_prompt,
            workspace_manager=workspace_manager,
            explicit_only_agents=explicit_only_agents,
            agent_bound_tools=agent_bound_tools,
            input_probe_client=input_probe_client,
            input_probe_model_cfg=input_probe_model_cfg,
        )

    def _create_model_fn(self, message_pair_id: Optional[str] = None):
        """创建模型实例的工厂函数（每次调用返回新实例）。

        message_pair_id 非空时模型请求带 app_serial_number 请求头
        （OIA-AGENTSCOPE-{message_pair_id}）。
        """
        default_model_cfg = self._model_config.get("models", {}).get("default", {})
        return create_model_from_config(
            default_model_cfg, message_pair_id=message_pair_id or ""
        )

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
                summary_client=self._summary_client,
                summary_model_config=self._summary_model_cfg,
            )

    async def _capture_tokens_from_stream(
        self, source, round_state: RoundState
    ):
        """转发 SSE 事件流，旁路解析 MODEL_CALL_END 事件累积真实 token。

        token 累积写入本轮 RoundState（每请求独立，并发请求互不覆盖）。
        agentscope 的事件以 "data: {...}\\n\\n" 字符串流转经本方法；先做
        子串快速判断（避免每条事件都 json.loads），命中后解析并校验 type，
        再累加 input_tokens / output_tokens。解析失败静默容错，不影响转发。
        """
        async with aclosing(source):
            async for ev in source:
                try:
                    if "MODEL_CALL_END" in ev:
                        payload = json.loads(ev.removeprefix("data: ").strip())
                        if payload.get("type") == "MODEL_CALL_END":
                            round_state.input_tokens += int(
                                payload.get("input_tokens") or 0
                            )
                            round_state.output_tokens += int(
                                payload.get("output_tokens") or 0
                            )
                except Exception:
                    logger.debug(
                        "[OrchestratorService] token 拦截解析失败", exc_info=True
                    )
                yield ev

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
    def _has_file_blocks(messages: List[Dict[str, Any]]) -> bool:
        """最后一条用户消息是否携带文件（data）块——纯文件轮判定。"""
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                return any(
                    isinstance(b, dict) and b.get("type") == "data"
                    for b in content
                )
            return False
        return False

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

    async def build_and_cache_user_config(
        self,
        user_id: str,
        jwt_token: str,
        permissions: dict,
        redis_client,
    ) -> dict:
        """登录时融合并缓存用户配置（委托 user_config_service，供 auth 路由调用）。"""
        return await user_config_build_and_cache(
            user_id, jwt_token, permissions, redis_client,
        )

    async def _load_config_bundle(self, user_id: str, redis_client) -> dict:
        """读取登录时缓存的融合配置，未命中 base-only 兜底（委托 user_config_service）。"""
        return await load_config_bundle(user_id, redis_client)

    async def _search_blocked_by_mng(
        self, user_id: Optional[str], redis_client
    ) -> bool:
        """mng 联网搜索权限校验：用户 skill_blacklist 含 bocha_search 返回 True。

        权限读取自 Redis（登录时 mng 下发的 permissions 原样缓存，
        key 见 auth_service._REDIS_KEY_PERMISSIONS）。未登录/无缓存/
        读取异常均按放行处理——与"权限服务异常不阻断对话"的既有
        降级策略一致；mng 拉黑的用户在下次登录时生效。
        """
        if not user_id or redis_client is None:
            return False
        try:
            from app.services.auth_service import get_user_permissions
            from app.services.mng_service import build_blacklist_codes

            perms_data = await get_user_permissions(redis_client, user_id)
            permissions = (perms_data or {}).get("permissions") or {}
            blacklist = build_blacklist_codes(
                permissions.get("skill_blacklist", [])
            )
            if _SEARCH_SKILL_NAME in blacklist:
                logger.info(
                    f"[OrchestratorService] 用户 {user_id} "
                    f"联网搜索已被 mng skill_blacklist 禁用"
                )
                return True
        except Exception:
            logger.exception(
                "[OrchestratorService] 读取用户联网搜索权限失败，按放行处理"
            )
        return False

    async def _resolve_workspace_task(
        self, ws_task: "asyncio.Task", round_state: RoundState
    ) -> tuple:
        """等待工作区准备任务完成。

        失败不向调用方抛：把异常转成给前端的错误文案，由调用方 yield error 事件，
        并把本轮状态标记为失败。文案与串行版本保持一致，避免前端出现新的错误形态。

        Returns:
            (registry, agent_factory, error_message)；失败时前两项为 None。
        """
        try:
            registry, agent_factory = await ws_task
            return registry, agent_factory, None
        except Exception as e:
            logger.exception("[OrchestratorService] 工作区准备失败")
            round_state.success = False
            return None, None, f"环境准备失败: {str(e)}"

    def _build_intent_components(self, fused: dict) -> tuple:
        """构建请求级改写器与识别器（纯内存，不依赖工作区）。

        显式触发智能体（explicit_only_agents）对应的意图在此时剔除，
        仅影响意图识别候选集；agent/skill 注册不受影响，
        用户显式选择该智能体时直答路径仍可用。

        Returns:
            (rewriter, recognizer)
        """
        intent_configs = [
            IntentConfig(**item)
            for item in fused["merged_intents"]
            if item.get("agent") not in self._explicit_only_agents
        ]
        recognizer = IntentRecognizer(
            client=self._intent_client,
            model_config=self._intent_model_cfg,
            recognition_prompt=self._prompts.get("intent_recognition", ""),
            orchestration_prompt=self._prompts.get("intent_orchestration", ""),
            intent_configs=intent_configs,
            default_orchestration=fused["default_orchestration"],
        )
        rewriter = QueryRewriter(
            client=self._intent_client,
            model_config=self._intent_model_cfg,
            rewrite_prompt=self._prompts.get("rewrite", ""),
        )
        return rewriter, recognizer

    async def _prepare_workspace_components(
        self,
        fused: dict,
        user_id: str,
        redis_client,
        user_id_safe: str,
        session_id_safe: str,
        search_enabled: bool = True,
        skills: Optional[List[str]] = None,
        knowledge_bases: Optional[List[str]] = None,
        langfuse_service: Optional[Any] = None,
        message_pair_id: Optional[str] = None,
        session_service: Optional[Any] = None,
        round_state: Optional[RoundState] = None,
    ) -> tuple:
        """获取/创建工作区并组装注册表与工厂（委托 workspace_assembler）。

        本方法会被 run() 放进 asyncio.create_task，
        请求级状态只通过显式传入的 RoundState 传递，不写服务实例属性。

        Returns:
            (registry, agent_factory)
        """
        return await assemble_workspace_components(
            workspace_manager=self._workspace_manager,
            # 闭包绑定本轮 message_pair_id：模型实例创建时注入
            # app_serial_number 请求头（registry 侧仍以无参形式调用）
            create_model_fn=lambda: self._create_model_fn(message_pair_id),
            fused=fused,
            user_id=user_id,
            redis_client=redis_client,
            user_id_safe=user_id_safe,
            session_id_safe=session_id_safe,
            search_enabled=search_enabled,
            skills=skills,
            knowledge_bases=knowledge_bases,
            langfuse_service=langfuse_service,
            agent_bound_tools=self._agent_bound_tools,
            session_service=session_service,
            round_state=round_state,
        )

    def _span(self, langfuse_service: Any, name: str, input_dict: dict) -> Any:
        """统一 span 创建：启用 langfuse 时启动子 span，否则返回空 context manager。

        返回一个 context manager，with ... as span 使用；span 可能为 None。
        """
        if langfuse_service:
            return langfuse_service.start_span(name, input=input_dict)
        return _noop_ctx()

    async def _run_single_agent_path(
        self,
        registry: AgentRegistry,
        agent_factory: AgentFactory,
        agent_id: str,
        user_input: str,
        session_id: Optional[str],
        user_id: Optional[str],
        session_service: Any,
        langfuse_service: Any,
        round_state: RoundState,
    ) -> AsyncGenerator[str, None]:
        """单 agent 直接问答全流程：校验 → 加载状态 → 创建 → span 内流式执行 → 存状态 → 记标志 → yield summary。

        跳过改写/识别/编排，直接由指定 agent 回答；
        本轮 agent_ids / 成功标志写入 round_state。
        """
        yield self._event({
            "type": "orchestration_start",
            "mode": "direct",
            "agent_id": agent_id,
        })

        # 校验 agent_id 是否存在
        definition = registry.get_definition(agent_id)
        if not definition:
            yield self._event({
                "type": "error",
                "message": f"agent_id '{agent_id}' 不存在",
            })
            return

        intent = Intent(id=f"direct_{agent_id}", query=user_input, agent=agent_id)

        # 加载已有 AgentState
        agent_state = await load_agent_state(session_service, session_id, agent_id)

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

        # 执行单 agent 对话（span 内流式执行）
        user_msg = UserMsg(name="user", content=[TextBlock(text=user_input)])
        apply = None
        final_output_parts: List[str] = []

        async with AsyncExitStack() as stream_stack:
            agent_span = stream_stack.enter_context(self._span(
                langfuse_service,
                f"agent-{agent_id}",
                # 信息精简：query 注入的文件全文在 upload_files.parsed_content 留底
                {"intent": intent.id, "query": user_input[:2000]},
            ))
            from app.utils.agent_event_tracer import AgentEventTracer
            from app.utils.agent_event_stream import iter_agent_events
            from app.utils.event_serialization import serialize_agent_event
            from agentscope.event import ToolResultEndEvent as _ToolResultEnd
            from tools.bocha_search_tools import merge_bocha_snapshots
            tracer = AgentEventTracer(langfuse_service, agent_id, agent=agent)
            # 已下发 bocha_sum 累积：按 citation_id 幂等合并、升序，
            # 每次事件发"全量快照"而非增量，多次搜索时前端覆盖式渲染仍得到完整来源列表
            bocha_emitted: list = []
            # 上下文超长降级重试：最多重试 1 次，重试前清空历史对话上下文
            # （保留压缩摘要 summary），避免 400 直接中断整轮对话
            max_retries = 1
            for attempt in range(max_retries + 1):
                try:
                    def _on_reply_start(ev):
                        nonlocal apply
                        apply = AssistantMsg(
                            name=ev.name, content=[], id=ev.reply_id
                        )

                    events = await stream_stack.enter_async_context(aclosing(
                        iter_agent_events(agent, user_msg, tracer, _on_reply_start)
                    ))
                    async for event in events:
                        if apply:
                            apply.append_event(event)
                        yield f"data: {serialize_agent_event(event)}\n\n"
                        # 工具一结束立即下发 bocha_sum：正文 {n} 标注依赖来源列表，
                        # 先于正文 delta 到达前端，便于边渲染正文边映射引用
                        if isinstance(event, _ToolResultEnd):
                            early_bocha = tracer.consume_bocha_sum()
                            if early_bocha:
                                bocha_emitted = merge_bocha_snapshots(bocha_emitted, early_bocha)
                                yield (
                                    "data: "
                                    + json.dumps(
                                        {
                                            "type": "bocha_sum",
                                            "bocha_sum": bocha_emitted,
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                )

                    if apply:
                        text_parts = []
                        for block in apply.content:
                            if hasattr(block, "type") and block.type == "text":
                                text_parts.append(getattr(block, "text", str(block)))
                        final_output_parts.append("\n".join(text_parts).strip())

                    # 检测结果出来前仅暂存，落库由 chat_service 统一处理。
                    round_state.final_states[agent_id] = agent.state.model_dump(mode="json")

                    # emit 工具执行期间捕获的 citations（从 ToolResultEndEvent.metadata 累积）
                    citations = tracer.consume_citations()
                    if citations:
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "policy_qa_citations",
                                    "citations": citations,
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )

                    # emit 流末残余的 bocha_sum：残余快照按 citation_id 合并，无新编号时内容不变；
                    # 已发过且无残余时不再重复发（最后一个事件已含全量）
                    residual_bocha = tracer.consume_bocha_sum()
                    if residual_bocha:
                        bocha_emitted = merge_bocha_snapshots(bocha_emitted, residual_bocha)
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "bocha_sum",
                                    "bocha_sum": bocha_emitted,
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )

                    # 记录本轮参与的 agent_id + 成功标志（写入请求级状态）
                    round_state.agent_ids = [agent_id]
                    round_state.success = True

                    _safe_update_span(agent_span, {
                        "output": final_output_parts[0] if final_output_parts else "",
                        "success": True,
                        **tracer.summary(),
                    })
                    break  # 成功，跳出重试循环
                except Exception as e:
                    # 上下文超长（400）：清空历史对话上下文（保留 summary）后重试一次
                    if _is_context_length_error(e) and attempt < max_retries:
                        logger.warning(
                            "[OrchestratorService] 智能体 %s 上下文超长，"
                            "清空历史上下文后重试（第 %d 次）: %s",
                            agent_id, attempt + 1, e,
                        )
                        try:
                            agent.state.context.clear()
                        except Exception:
                            logger.warning(
                                "[OrchestratorService] 清空 agent.state.context 失败",
                                exc_info=True,
                            )
                        # 重置 apply，避免上次失败的部分结果污染
                        apply = None
                        continue
                    logger.exception(
                        f"[OrchestratorService] 单智能体 {agent_id} 执行异常"
                    )
                    round_state.success = False
                    yield self._event({
                        "type": "error",
                        "message": f"执行出错: {str(e)}",
                    })
                    return
                finally:
                    tracer.close()

        # yield summary 事件
        if final_output_parts:
            yield self._event({
                "type": "summary",
                "content": final_output_parts[0],
            })

    async def run(
        self,
        messages: List[Dict[str, Any]],
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        session_service: Optional[Any] = None,
        agent_id: Optional[str] = None,
        request: Optional[Request] = None,
        search_enabled: bool = True,
        langfuse_service: Optional[Any] = None,
        skills: Optional[List[str]] = None,
        knowledge_bases: Optional[List[str]] = None,
        message_pair_id: Optional[str] = None,
        round_state: Optional[RoundState] = None,
        input_probe_task: Optional["asyncio.Task"] = None,
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
            knowledge_bases: 前端所选知识库名列表（制度问答），
                空 = 全选；透传给 policy_qa 工具闭包做检索范围过滤
            message_pair_id: 本轮对话配对 id（非空时业务智能体 LLM 请求
                带 app_serial_number 请求头）
            round_state: 本轮运行时状态（agent_ids / success / token
                计数 / 消费的上传文件 id 的载体）；缺省时内部新建。
                由 generate_response 创建并传入、轮末持久化直接读取——
                单例服务上不存请求级状态，并发请求互不覆盖
            input_probe_task: 输入围栏 403 预检任务（与意图链路并行执行），
                执行智能体前汇合检查；命中时 yield input_llm_403 标记事件
                交由 chat_service 下发拦截文案。None 时跳过检查

        Yields:
            SSE 事件字符串（"data: {...}\n\n" 格式）
        """
        user_input = self._extract_last_user_message(messages)
        history = self._extract_history(messages)

        # 本轮运行时状态：每请求独立对象（并发请求互不覆盖），未传时新建
        rs = round_state or RoundState()
        # 轮初重置（容忍调用方传入复用对象；fresh 对象时等价于初值）
        rs.agent_ids = []
        rs.success = True
        rs.input_tokens = 0
        rs.output_tokens = 0
        rs.consumed_upload_ids = []
        rs.restored_files.clear()
        rs.completed_files.clear()
        rs.final_states.clear()

        if not user_input:
            if self._has_file_blocks(messages):
                # 纯文件轮：前端只传 data 块（text 为空），补默认提问驱动
                # 编排；上传文件的解析内容由后续 load_upload_context 按
                # 会话未绑定文件注入提示词，不依赖 data 块本身
                user_input = FILE_ONLY_DEFAULT_QUESTION
            else:
                yield self._event({"type": "error", "message": "未检测到有效用户输入"})
                return

        # ① 装配请求级配置组件（纯内存；workspace 获取/创建并行化，见下方 ws_task）
        redis_client = None
        if request is not None:
            redis_client = getattr(request.app.state, "redis_client", None)

        # mng 权限管控：用户 skill_blacklist 含 bocha_search 时强制关闭
        # 联网搜索（工具注入与技能装载双通道同步收口）；前端
        # search_enabled 开关仅在 mng 未拉黑的前提下生效
        if search_enabled and await self._search_blocked_by_mng(
            user_id, redis_client
        ):
            search_enabled = False

        user_id_safe = user_id or "anonymous"
        session_id_safe = session_id or f"ephemeral-{user_id_safe}"
        try:
            fused = await self._load_config_bundle(user_id, redis_client)
            rewriter, recognizer = self._build_intent_components(fused)
        except Exception as e:
            # 配置装配失败不能让 SSE 流以 ASGI 异常中断，转为 error 事件
            logger.exception("[OrchestratorService] 请求级配置装配失败")
            rs.success = False
            yield self._event({
                "type": "error",
                "message": f"环境准备失败: {str(e)}",
            })
            return

        # 工作区准备与意图链路并行：workspace 直到编排执行阶段才被消费，
        # 这段时间足以覆盖改写 + 意图识别 + 意图编排。
        ws_task = asyncio.create_task(
            self._prepare_workspace_components(
                fused=fused,
                user_id=user_id,
                redis_client=redis_client,
                user_id_safe=user_id_safe,
                session_id_safe=session_id_safe,
                search_enabled=search_enabled,
                skills=skills or [],
                knowledge_bases=knowledge_bases or [],
                langfuse_service=langfuse_service,
                message_pair_id=message_pair_id,
                session_service=session_service,
                round_state=rs,
            ),
            name="workspace-prepare",
        )
        try:
            async with aclosing(self._run_with_workspace_task(
                ws_task=ws_task,
                rewriter=rewriter,
                recognizer=recognizer,
                user_input=user_input,
                history=history,
                messages=messages,
                session_id=session_id,
                user_id=user_id,
                session_service=session_service,
                agent_id=agent_id,
                langfuse_service=langfuse_service,
                request=request,
                round_state=rs,
                input_probe_task=input_probe_task,
            )) as stream:
                async for event_str in stream:
                    yield event_str
        finally:
            # 客户端断连会让本生成器被提前关闭：未完成则取消，已完成则消费异常，
            # 否则事件循环会报 "Task exception was never retrieved"
            if not ws_task.done():
                ws_task.cancel()
            elif not ws_task.cancelled():
                ws_task.exception()

    async def _run_with_workspace_task(
        self,
        ws_task: "asyncio.Task",
        rewriter: QueryRewriter,
        recognizer: IntentRecognizer,
        user_input: str,
        history: List[dict],
        messages: List[Dict[str, Any]],
        session_id: Optional[str],
        user_id: Optional[str],
        session_service: Optional[Any],
        agent_id: Optional[str],
        langfuse_service: Optional[Any],
        request: Any = None,
        round_state: RoundState = None,
        input_probe_task: Optional["asyncio.Task"] = None,
    ) -> AsyncGenerator[str, None]:
        """在等待工作区 task 的同时跑完意图链路，再执行编排。

        拆成独立方法是为了让 run() 的 finally 能无条件覆盖本方法的全部提前返回路径
        （return / 异常 / 生成器被关闭），保证 ws_task 不会悬挂；
        本轮运行时状态（agent_ids / success / token / 消费文件 id）写入 round_state。
        """
        # ② 单 agent 短路路径（跳过改写→识别→编排，直接由指定 agent 回答）
        if agent_id:
            registry, agent_factory, ws_err = await self._resolve_workspace_task(
                ws_task, round_state
            )
            if ws_err:
                yield self._event({"type": "error", "message": ws_err})
                return
            # 指定的 agent 不在当前注册表中（权限/mng 配置变更等），
            # 回退到编排路径（意图识别重新选 agent），避免直接报错中断对话。
            if not registry.get_definition(agent_id):
                logger.warning(
                    "[OrchestratorService] 指定的 agent_id=%s 不在注册表中，"
                    "回退到编排路径 session=%s",
                    agent_id, session_id,
                )
                agent_id = None
            else:
                # 输入围栏预检结果汇合（任务已与工作区准备并行执行）
                if input_probe_task is not None and await input_probe_task:
                    yield self._event({"type": "input_llm_403"})
                    return
                # 等待解析中的上传文件（向前端发 TOOL_CALL 事件对展示等待过程）
                async for ev in wait_for_upload_parsing(request, session_id):
                    yield ev
                persisted = await persist_upload_files_to_workspace(
                    request, session_id, user_id, self._workspace_manager
                )
                round_state.consumed_upload_ids = persisted["file_ids"]
                upload_ctx = await load_upload_context(
                    request, session_id, persisted["sandbox_files"]
                )
                user_input = append_upload_context(user_input, upload_ctx)
                async with aclosing(self._capture_tokens_from_stream(
                    self._run_single_agent_path(
                        registry, agent_factory, agent_id, user_input,
                        session_id, user_id, session_service, langfuse_service,
                        round_state,
                    ),
                    round_state,
                )) as stream:
                    async for ev in stream:
                        yield ev
                return

        # ③ 查询改写（联系上下文，失败降级为原始输入）。
        # 有未消费的上传文件时跳过改写：文件解析内容将在编排前注入 query，
        # 原始问题已足够完整，改写反而可能引入偏差
        skip_rewrite = await has_unbound_uploads(request, session_id)
        with self._span(
            langfuse_service, "query-rewrite",
            {"original": user_input, "history_len": len(history)},
        ) as rw_span:
            if skip_rewrite:
                rewritten, rw_prompts = user_input, {}
                _rw_extra = {"skipped": "upload-file"}
            else:
                try:
                    rewritten, rw_prompts = await rewriter.rewrite(user_input, history)
                    _rw_extra = {}
                except Exception as e:
                    logger.exception("[OrchestratorService] 查询改写失败，使用原始输入")
                    rewritten, rw_prompts = user_input, {}
                    _rw_extra = {"degraded": True, "reason": type(e).__name__, "detail": str(e)[:500]}
            _safe_update_span(rw_span, {"rewritten": rewritten, "prompts": rw_prompts, **_rw_extra})

        yield self._event({
            "type": "query_rewritten",
            "original": user_input,
            "rewritten": rewritten,
            **({"skipped_reason": "upload-file"} if skip_rewrite else {}),
        })

        # ④ 意图识别（第一次 LLM，失败降级为 general_chat）
        yield self._event({
            "type": "intent_step", "phase": "recognition", "status": "started",
            "message": "正在识别意图...",
        })
        with self._span(
            langfuse_service, "intent-recognition",
            {"query": rewritten, "history_len": len(history)},
        ) as rec_span:
            # 本轮上传文件清单提示：约束子 query 文件指代只能用本轮文件，
            # 防止 LLM 从历史回复脑补旧文件名（构建失败为空串，不影响识别）。
            # 仅为空时按原签名调用，兼容无 file_context 参数的旧实现/测试替身
            file_hint = await build_recognizer_file_hint(request, session_id)
            _rec_kwargs = {"file_context": file_hint} if file_hint else {}
            try:
                intents, rec_prompts = await recognizer.recognize_intents(
                    rewritten, history, **_rec_kwargs
                )
                _rec_extra = {"file_hint": file_hint[:500]} if file_hint else {}
            except Exception as e:
                logger.exception("[OrchestratorService] 意图识别失败，降级为 general_chat")
                intents = [Intent(id="general_chat", query=rewritten, agent="general_agent")]
                rec_prompts = {}
                _rec_extra = {"degraded": True, "reason": type(e).__name__, "detail": str(e)[:500]}
            _safe_update_span(rec_span, {
                "intents": [{"id": i.id, "agent": i.agent} for i in intents],
                "prompts": rec_prompts, **_rec_extra,
            })
        yield self._event({
            "type": "intent_step", "phase": "recognition", "status": "done",
            "intents": [{"id": i.id, "query": i.query, "agent": i.agent} for i in intents],
        })

        # ⑤ 意图编排（第二次 LLM，失败降级为 independent）
        yield self._event({
            "type": "intent_step", "phase": "orchestration", "status": "started",
            "message": "正在决策编排策略...",
        })
        with self._span(
            langfuse_service, "intent-orchestration",
            {"intents_count": len(intents)},
        ) as orch_span:
            try:
                relation, execution_order, orch_prompts = await recognizer.plan_orchestration(rewritten, intents)
                _orch_extra = {}
            except Exception as e:
                logger.exception("[OrchestratorService] 意图编排失败，降级为 independent")
                relation, execution_order, orch_prompts = "independent", [], {}
                _orch_extra = {"degraded": True, "reason": type(e).__name__, "detail": str(e)[:500]}
            _safe_update_span(orch_span, {
                "relation": relation, "execution_order": execution_order,
                "prompts": orch_prompts, **_orch_extra,
            })
        # 按 execution_order 重排 intents（校验长度一致才应用，否则按原顺序）
        if execution_order and len(execution_order) == len(intents):
            intents = [intents[i] for i in execution_order]
        intent_result = IntentResult(
            rewritten_query=rewritten, intents=intents,
            relation=relation, execution_order=execution_order,
        )
        yield self._event({
            "type": "intents_recognized",
            "intents": [
                {"id": i.id, "agent": i.agent, "query": i.query}
                for i in intent_result.intents
            ],
            "relation": intent_result.relation,
        })

        # 输入围栏预检结果汇合：预检任务与意图链路并行执行完毕，命中则
        # 交由 chat_service 下发拦截文案并结束本轮（不再等待工作区/上传
        # 文件；沙箱准备任务由 run() 的 finally 收尾）
        if input_probe_task is not None and await input_probe_task:
            yield self._event({"type": "input_llm_403"})
            return

        # 编排前汇合：此时意图链路已跑完，工作区大概率已就绪
        registry, agent_factory, ws_err = await self._resolve_workspace_task(
            ws_task, round_state
        )
        if ws_err:
            yield self._event({"type": "error", "message": ws_err})
            return

        # 等待解析中的上传文件（向前端发 TOOL_CALL 事件对展示等待过程）
        async for ev in wait_for_upload_parsing(request, session_id):
            yield ev
        # 上传文件解析内容注入：意图识别已通过，真正进入问答阶段。
        # 附加到每个 intent 的 query（pipeline/react 均以 intent.query 作为 agent 任务输入）
        persisted = await persist_upload_files_to_workspace(
            request, session_id, user_id, self._workspace_manager
        )
        round_state.consumed_upload_ids = persisted["file_ids"]
        upload_ctx = await load_upload_context(
            request, session_id, persisted["sandbox_files"]
        )
        if upload_ctx:
            for intent in intent_result.intents:
                intent.query = append_upload_context(intent.query, upload_ctx)

        # ⑥ 选择编排器并加载各 agent 状态
        mode = recognizer.get_orchestration_mode(intent_result)
        orchestrator = self._create_orchestrator(mode, agent_factory)

        agent_states: Dict[str, AgentState] = {}
        for intent in intent_result.intents:
            aid = intent.agent or "general_agent"
            agent_states[aid] = await load_agent_state(
                session_service, session_id, aid,
            )

        # ⑥ 执行编排（内部实时 yield SSE 事件）
        async with aclosing(self._capture_tokens_from_stream(
            orchestrator.run(
                intent_result,
                session_id=session_id,
                agent_states=agent_states,
                langfuse_service=langfuse_service,
            ),
            round_state,
        )) as stream:
            async for event_str in stream:
                yield event_str

        # ⑦ 汇总编排结果并暂存各 agent 的待检状态
        # agent_id 列表（去重保序）；成功标志从编排结果派生
        # （任一 TaskResult.success=False 则整体 False；空结果时 all([])=True）
        round_state.agent_ids = list(dict.fromkeys(
            r.agent_id for r in orchestrator._last_results if r.agent_id
        ))
        round_state.success = all(
            r.success for r in orchestrator._last_results
        )
        for r in orchestrator._last_results:
            if r.final_state:
                round_state.final_states[r.agent_id] = r.final_state
