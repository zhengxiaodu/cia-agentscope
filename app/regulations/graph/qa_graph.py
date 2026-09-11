"""LangGraph QA 图构建 — 节点 + compile。"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import RunnableConfig
from langchain_core.prompts import ChatPromptTemplate

from app.regulations.config import (
    REGULATIONS_QUERY_UNDERSTANDING_ENABLED,
    REGULATIONS_QUERY_REWRITE_HYDE_ENABLED,
    REGULATIONS_QUERY_REWRITE_STEPBACK_ENABLED,
    REGULATIONS_QUERY_REWRITE_LLM_CLASSIFY_ENABLED,
    REGULATIONS_QUERY_REWRITE_BUSINESS_CLASSIFY_ENABLED,
    REGULATIONS_QUERY_REWRITE_BUSINESS_INTENTS_ENABLED,
)
from app.regulations.graph.state import QAState
from app.regulations.graph.prompts import (
    REWRITE_SYSTEM_PROMPT,
    REWRITE_USER_TEMPLATE,
    GENERATION_SYSTEM_PROMPT,
    GENERATION_USER_TEMPLATE,
)
from app.regulations.services.query_understanding.query_understand import (
    understand_query,
    collect_retrieval_queries,
    extract_entities,
    check_domain,
    QUERY_REWRITE_SYSTEM_PROMPT,
)
from app.regulations.services.query_understanding.memory import SessionMemory
from app.regulations.providers.llm import get_rewrite_llm

logger = logging.getLogger(__name__)

# 模型配置（resolve_regulations_model 的返回值）。模块级注入：
# 服务初始化时调用 set_model_cfg，understand/refuse 节点构建改写 LLM 时读取。
_model_cfg: dict | None = None


def set_model_cfg(cfg: dict) -> None:
    """注入模型配置，供改写/拒答 LLM 构建；须在首个请求前调用。"""
    global _model_cfg
    _model_cfg = cfg


def _require_model_cfg() -> dict:
    """取已注入的模型配置；未初始化时抛错（服务启动时应先 set_model_cfg）。"""
    if _model_cfg is None:
        raise RuntimeError(
            "regulations QA 模型配置未初始化：请先调用 "
            "app.regulations.graph.qa_graph.set_model_cfg()"
        )
    return _model_cfg


# ══════════════════════════════════════════════════════════════
# 节点函数（独立异步，通过 RunnableConfig 注入依赖）
# ══════════════════════════════════════════════════════════════

async def _legacy_rewrite(state: QAState, config: RunnableConfig) -> dict[str, Any]:
    """旧逻辑：多轮追问 → 独立查询。无历史时跳过 LLM 调用。"""
    history = state.get("history") or []

    if not history:
        logger.debug("rewrite: 无历史消息，跳过")
        return {"standalone_query": state["query"]}

    llm = config["configurable"]["llm"]
    prompt = ChatPromptTemplate.from_messages([
        ("system", REWRITE_SYSTEM_PROMPT),
        ("user", REWRITE_USER_TEMPLATE),
    ])

    # 多轮上下文（轮数由 HISTORY_ROUNDS 控制，_load_history 已按该值截取）
    history_text = "\n".join(
        f"用户: {m.get('query', '')}\n助手: {str(m.get('answer', ''))[:200]}"
        for m in history
    )

    chain = prompt | llm
    response = await chain.ainvoke({
        "history": history_text,
        "query": state["query"],
    })
    rewritten = response.content.strip() if hasattr(response, "content") else str(response).strip()
    logger.info(f"rewrite: '{state['query'][:50]}...' → '{rewritten[:50]}...'")
    return {"standalone_query": rewritten}


# 子步骤进度文案 → 后台日志（不再推前端 SSE，前端 rewrite 阶段统一显示“正在理解问题...”）
_PROGRESS_LABELS = {
    "分类": "正在分类问题…",
    "业务意图分类": "正在识别业务意图…",
    "改写": "正在改写检索语句…",
    "HyDE": "正在生成假设答案…",
    "Step-back": "正在抽象问题…",
    "拆解对比": "正在拆解对比问题…",
}


async def understand_node(state: QAState, config: RunnableConfig) -> dict[str, Any]:
    """Query 理解：分类 + 实体 + 业务意图 + 改写 + HyDE/Step-back，产出多路检索 query 或澄清。"""
    # ── 领域门禁：出域问题（百科等）直接拒答，不进理解/检索 ──
    # 只在会话首轮（无历史）判断：多轮追问视为延续既定主题，不重复判域。
    if not state.get("history"):
        entities = extract_entities(state["query"])
        rewrite_llm = get_rewrite_llm(_require_model_cfg())

        async def _domain_llm(prompt: str) -> str:
            resp = await rewrite_llm.ainvoke(prompt)
            return (resp.content or "").strip() if hasattr(resp, "content") else str(resp).strip()

        if not await check_domain(state["query"], entities, _domain_llm):
            return {
                "refuse": True,
                "standalone_query": state["query"],
                "retrieval_queries": [],
                "clarification": None,
                "qtype": "out_of_domain",
                "entities": entities,
                "need_full_doc": False,
            }

    # per-request 开关：False → 跳过理解，直接用原 query 检索。
    # 缺省视为 True（不短路），兼容直接构造 state 的调用方/测试。
    if not state.get("enable_query_understanding", True):
        return {
            "standalone_query": state["query"],
            "retrieval_queries": [state["query"]],
            "clarification": None,
            "qtype": None,
            "entities": {},
            "need_full_doc": False,
        }

    if not REGULATIONS_QUERY_UNDERSTANDING_ENABLED:
        return await _legacy_rewrite(state, config)

    # 重建会话记忆（从 history 重放，无状态）
    memory = SessionMemory(user_id=state.get("user_id", ""))
    for m in state.get("history") or []:
        ents = extract_entities(m.get("query", ""), memory)
        if (ents.get("policy_name") and memory.current_policy
                and ents["policy_name"][0] != memory.current_policy):
            memory.clear_topic()
        memory.update_entities(ents)
        memory.add_turn("user", m.get("query", ""))
        memory.add_turn("assistant", m.get("answer", "") or "")

    rewrite_llm = get_rewrite_llm(_require_model_cfg())

    async def llm_call_fn(prompt: str) -> str:
        resp = await rewrite_llm.ainvoke([
            ("system", QUERY_REWRITE_SYSTEM_PROMPT),
            ("user", prompt),
        ])
        return (resp.content or "").strip()

    async def on_progress(label: str) -> None:
        # 子阶段进度只落后台日志，不推前端（前端 rewrite 阶段统一显示“正在理解问题...”）
        logger.info(f"[query-understand] {_PROGRESS_LABELS.get(label, label)}")

    result = await understand_query(
        query=state["query"],
        memory=memory,
        use_llm_rewrite=True,
        llm_call_fn=llm_call_fn,
        use_hyde=REGULATIONS_QUERY_REWRITE_HYDE_ENABLED,
        use_stepback=REGULATIONS_QUERY_REWRITE_STEPBACK_ENABLED,
        use_llm_classify=REGULATIONS_QUERY_REWRITE_LLM_CLASSIFY_ENABLED,
        use_llm_business_classify=REGULATIONS_QUERY_REWRITE_BUSINESS_CLASSIFY_ENABLED,
        use_business_intents=REGULATIONS_QUERY_REWRITE_BUSINESS_INTENTS_ENABLED,
        on_progress=on_progress,
    )

    rewritten = result.get("rewritten") or [state["query"]]
    standalone_query = rewritten[0] if rewritten else state["query"]
    retrieval_queries = collect_retrieval_queries(result) or [state["query"]]

    logger.info(
        f"[query-understand] qtype={result.get('type')} "
        f"entities={result.get('entities')} "
        f"queries={len(retrieval_queries)} "
        f"llm_calls={result.get('llm_calls')} llm_ms={result.get('llm_total_ms')} "
        f"elapsed_ms={result.get('elapsed_ms')} "
        f"clarify={bool((result.get('clarification') or {}).get('required'))}"
    )

    return {
        "standalone_query": standalone_query,
        "retrieval_queries": retrieval_queries,
        "clarification": result.get("clarification"),
        "qtype": result.get("type"),
        "entities": result.get("entities"),
        "need_full_doc": result.get("need_full_doc", False),
    }


async def retrieve_node(state: QAState, config: RunnableConfig) -> dict[str, Any]:
    """多路检索：遍历 retrieval_queries，交由 KBClient.retrieve_multi 合并去重。"""
    kb_client = config["configurable"]["kb_client"]
    kb_ids = state.get("kb_ids") or []

    if not kb_ids:
        logger.warning("retrieve: kb_ids 为空")
        return {"retrieved_docs": [], "kb_names": []}

    queries = state.get("retrieval_queries") or [state.get("standalone_query") or state["query"]]

    results = await kb_client.retrieve_multi(
        kb_ids=kb_ids,
        queries=queries,
        top_k=state.get("top_k", 20),
        rerank_top_k=state.get("rerank_top_k", 5),
        score_threshold=state.get("score_threshold", 0.0),
        user_id=state.get("user_id", ""),
    )

    logger.info(f"retrieve: {len(results)} docs returned")
    return {"retrieved_docs": results}


async def generate_node(state: QAState, config: RunnableConfig) -> dict[str, Any]:
    """基于检索结果流式生成答案。

    内部使用 chain.astream() → LangGraph astream_events 透传
    on_chat_model_stream 事件给 ChatService → SSE content_block_delta。
    """
    retrieved_docs = state.get("retrieved_docs") or []

    if not retrieved_docs:
        logger.warning("generate: 无检索结果")
        return {"answer": "未检索到相关内容，无法回答。"}

    llm = config["configurable"]["llm"]
    prompt = ChatPromptTemplate.from_messages([
        ("system", GENERATION_SYSTEM_PROMPT),
        ("user", GENERATION_USER_TEMPLATE),
    ])

    # 组织上下文
    parts: list[str] = []
    for i, doc in enumerate(retrieved_docs):
        name = doc.get("document_name", "未知文档")
        p_start = doc.get("page_start") or "?"
        p_end = doc.get("page_end") or "?"
        content = doc.get("content", "")
        parts.append(f"[{i + 1}] ({name}, p{p_start}-{p_end})\n{content}")

    context_text = "\n\n".join(parts)
    chain = prompt | llm

    full_thinking = ""
    full_answer = ""
    async for chunk in chain.astream({
        "context": context_text,
        # 多轮追问用改写后的独立查询：否则 LLM 拿到"那第二条呢?"这类无历史
        # 追问，无从定位，答案错/幻觉（retrieve 已按 standalone_query 召回）。
        "query": state.get("standalone_query") or state["query"],
    }):
        # 收集 thinking content（来自 additional_kwargs）
        thinking = ""
        if hasattr(chunk, "additional_kwargs"):
            thinking = chunk.additional_kwargs.get("reasoning_content", "") or ""
        if thinking:
            full_thinking += thinking

        if hasattr(chunk, "content") and chunk.content:
            full_answer += chunk.content

    # 剔除越界 [N] 引用标记（流式已发出的原文不回溯，落库/兜底用清洗后文本）
    from app.regulations.services.citation_service import CitationService
    full_answer = CitationService.sanitize_references(full_answer, len(retrieved_docs))

    logger.info(f"generate: answer length={len(full_answer)}, thinking length={len(full_thinking)}")
    return {"answer": full_answer, "thinking_content": full_thinking}


async def clarify_node(state: QAState, config: RunnableConfig) -> dict[str, Any]:
    """澄清分支：不检索、不调 LLM，直接把反问句作为 answer。"""
    clarification = state.get("clarification") or {}
    questions = clarification.get("questions") or []
    answer = "、".join(questions) if questions else "请说明你想了解的具体政策，以及希望解决的问题是什么？"
    return {"answer": answer, "retrieved_docs": []}


_REFUSE_SYSTEM_PROMPT = """你是中债公司（中央国债登记结算）内部制度问答助手。用户提出了一个与法律、制度、公司业务无关的问题。请友好地告知用户：你只能回答与公司法律、制度、业务相关的问题，并引导用户提出相关的问题（如制度条款、业务流程、报销、考勤、支付结算等）。语气自然友好，2-3 句话即可，不要编造制度内容。"""


async def refuse_node(state: QAState, config: RunnableConfig) -> dict[str, Any]:
    """领域拒答：LLM 生成友好引导语，不进检索。"""
    llm = get_rewrite_llm(_require_model_cfg())
    resp = await llm.ainvoke([
        ("system", _REFUSE_SYSTEM_PROMPT),
        ("user", state["query"]),
    ])
    answer = (resp.content or "").strip() if hasattr(resp, "content") else str(resp).strip()
    return {"answer": answer, "refuse": True, "retrieved_docs": []}


def route_after_understand(state: QAState) -> str:
    if state.get("refuse"):
        return "refuse"
    clarification = state.get("clarification") or {}
    if clarification.get("required"):
        return "clarify"
    return "retrieve"


# ══════════════════════════════════════════════════════════════
# Graph 编译
# ══════════════════════════════════════════════════════════════

def build_qa_graph() -> StateGraph:
    """构建并编译 QA 有向图。"""
    workflow = StateGraph(QAState)

    workflow.add_node("rewrite", understand_node)      # noqa: F821
    workflow.add_node("clarify", clarify_node)
    workflow.add_node("refuse", refuse_node)
    workflow.add_node("retrieve", retrieve_node)
    workflow.add_node("generate", generate_node)

    workflow.set_entry_point("rewrite")
    workflow.add_conditional_edges(
        "rewrite", route_after_understand,
        {"clarify": "clarify", "retrieve": "retrieve", "refuse": "refuse"},
    )
    workflow.add_edge("clarify", END)
    workflow.add_edge("refuse", END)
    workflow.add_edge("retrieve", "generate")
    workflow.add_edge("generate", END)

    return workflow.compile(checkpointer=MemorySaver())
