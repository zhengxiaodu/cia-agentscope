"""Langfuse 埋点元数据的纯函数 — 无 SDK / IO 依赖，便于单测。

数据大盘埋点规范（数据大盘接口埋点及策略.md）要求 trace 携带
resultType（empty/normal）与 citedDocs[]（docId 列表），二者在检索
完成后才可知，故抽出纯函数供 trace 结束前写 output 阶段调用。
"""

from __future__ import annotations

# 拒答句式：LLM 按 prompt 约定，在资料不相关/信息不足时输出这类话术，视为空应答。
_EMPTY_MARKERS = ("无法回答",)


def compute_result_type(answer: str | None, retrieved_count: int = 1) -> str:
    """按回答判定 resultType：empty / normal。

    当前制度问答无「拒答（refused）」流程，故只区分 empty / normal。
    empty 判定：
      1. 回答为空；
      2. 检索无结果（占位回答「未检索到相关内容」）；
      3. 回答命中拒答句式（如「根据现有资料无法回答」）。
    retrieved_count 默认 1，便于只按 answer 判定的场景。
    """
    if not (answer or "").strip():
        return "empty"
    if retrieved_count <= 0:
        return "empty"
    if any(marker in answer for marker in _EMPTY_MARKERS):
        return "empty"
    return "normal"


def build_cited_docs(resources: list[dict] | None) -> list[str]:
    """从检索结果提取去重后的 docId 列表（citedDocs[]）。

    保持出现顺序，去重并过滤空值；resources 为 None/空时返回空列表。
    """
    seen: set[str] = set()
    docs: list[str] = []
    for r in resources or []:
        doc_id = (r or {}).get("document_id", "") or ""
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            docs.append(doc_id)
    return docs
