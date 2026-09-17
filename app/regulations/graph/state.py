"""LangGraph QAState 定义。"""

from __future__ import annotations

from typing import Any, TypedDict


class QAState(TypedDict, total=False):
    """QA 流程状态。

    节点通过返回 dict[str, Any] 更新部分字段，LangGraph 自动合并。
    """
    query: str                      # 用户问题原文
    user_id: str                    # 当前用户标识（用于 KB 用户上下文鉴权）
    conversation_id: str            # 会话 ID
    history: list[dict[str, Any]]   # 历史消息列表
    kb_ids: list[str]               # 检索目标知识库 ID 列表
    kb_names: list[str]             # 知识库名称（retrieve 节点填充，用于前端展示）
    top_k: int                      # 召回数量
    rerank_top_k: int               # rerank 后保留数量
    score_threshold: float           # 置信度阈值（0.0-1.0）
    enable_query_understanding: bool  # 是否启用 query 理解与改写；False 直接用原 query 检索
    standalone_query: str           # rewrite 后产出（独立查询文本）
    retrieval_queries: list[str]     # 所有待检索 query（主改写 + multi-query + hyde + stepback + comparison 子问题）
    clarification: dict | None       # {"required": bool, "questions": [str], "missing_slots": [...]}
    refuse: bool                     # 领域门禁：出域直接拒答（不进检索）
    qtype: str                       # 分类结果（观测/日志）
    entities: dict                   # 提取实体（观测/日志）
    need_full_doc: bool              # 全文意图标志
    retrieved_docs: list[dict[str, Any]]  # retrieval 后产出
    answer: str                     # generation 后产出
    thinking_content: str           # 思考过程全文
    error: str | None               # 异常信息
