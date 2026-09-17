"""多路检索结果合并去重。"""

from __future__ import annotations


def merge_retrieval_results(rows: list[dict], top_n: int) -> list[dict]:
    """按 segment_id 去重（缺则退化 document_id+content），保留 score 最高的一条，降序取 top_n。"""
    seen: dict[str, dict] = {}
    for r in rows:
        key = r.get("segment_id") or (r.get("document_id", "") + "\x00" + r.get("content", ""))
        if key not in seen:
            seen[key] = dict(r)
        elif (r.get("score") or 0.0) > (seen[key].get("score") or 0.0):
            seen[key] = dict(r)

    merged = list(seen.values())
    merged.sort(key=lambda x: x.get("score") or 0.0, reverse=True)
    merged = merged[:top_n]
    for i, r in enumerate(merged):
        r["position"] = i + 1
    return merged
