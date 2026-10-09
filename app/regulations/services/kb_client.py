"""知识库内部 API 客户端 — 调用 policy-kb-api 的召回接口。"""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx

from app.regulations.config import (
    REGULATIONS_KB_API_BASE_URL,
    REGULATIONS_KB_INTERNAL_TOKEN,
)
from app.regulations.providers.trace import get_serial
from app.regulations.providers.langfuse import get_langfuse
from app.regulations.schemas import RetrieverResource
from app.regulations.services.retrieval_merge import merge_retrieval_results

logger = logging.getLogger(__name__)


class KBClientError(Exception):
    """知识库 API 调用异常。"""

    def __init__(self, message: str, code: str = "retrieval_error", request_id: str = ""):
        self.code = code
        self.message = message
        self.request_id = request_id
        self.status_code = 500
        super().__init__(message)


class KBClient:
    """知识库内部 API 客户端。

    内置超时与重试：连接超时 5s，读取超时 30s，5xx 错误自动重试 2 次（间隔 1s）。
    """

    def __init__(self) -> None:
        self.base_url = REGULATIONS_KB_API_BASE_URL.rstrip("/")
        self.token = REGULATIONS_KB_INTERNAL_TOKEN
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )

    async def retrieve(
        self,
        kb_ids: list[str],
        query: str,
        top_k: int = 20,
        rerank_top_k: int = 5,
        score_threshold: float = 0.0,
        user_id: str = "",
        max_retries: int = 2,
    ) -> list[dict]:
        """调用 POST /api/v1/retrieval（单层鉴权：仅内部 token，user_id 作审计透传）。"""
        return await self._retrieve_once(
            kb_ids, query, top_k, rerank_top_k, score_threshold, user_id, max_retries,
        )

    async def _retrieve_once(
        self,
        kb_ids: list[str],
        query: str,
        top_k: int,
        rerank_top_k: int,
        score_threshold: float,
        user_id: str,
        max_retries: int,
    ) -> list[dict]:
        """单次检索：headers + payload + 重试 + 字段映射。"""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Request-ID": str(uuid.uuid4()),
        }
        if serial := get_serial():
            headers["app_serial_number"] = serial
        payload = {
            "kb_ids": kb_ids,
            "query": query,
            "top_k": top_k,
            # KB 契约 rerank_top_k 上限 20（QA 侧 schema 允许到 50，传大值 KB 会 422）
            "rerank_top_k": min(rerank_top_k, 20),
            "score_threshold": score_threshold,
            "retrieval_mode": "hybrid",
        }
        if user_id:
            payload["user_id"] = user_id

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = await self.client.post(
                    "/api/v1/retrieval", json=payload, headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("code", -1) != 0:
                    req_id = data.get("request_id", "")
                    raise KBClientError(
                        f"[{req_id}] {data.get('message', 'unknown error')}",
                        code="retrieval_error",
                        request_id=req_id,
                    )

                # 记录检索耗时（字段对齐 KB API: embedding / search / rerank / total）
                lat = data.get("data", {}).get("latency_ms", {})
                logger.info(
                    f"[{data.get('request_id', '')}] retrieval latency: "
                    f"embed={lat.get('embedding')}ms "
                    f"search={lat.get('search')}ms "
                    f"rerank={lat.get('rerank')}ms total={lat.get('total')}ms"
                )

                # 字段映射：KB API → 内部统一格式（RetrieverResource / Dify 字段名）
                results = data.get("data", {}).get("results", [])
                return [
                    {
                        "position": i + 1,
                        "dataset_id":    r.get("kb_id", ""),
                        "dataset_name":  r.get("kb_name", ""),
                        "document_id":   r.get("doc_id", ""),
                        "document_name": r.get("doc_name", ""),
                        "segment_id":    r.get("chunk_id", ""),
                        "content":       r.get("content", ""),
                        "score":         r.get("score", 0.0),
                        "page_start":    r.get("page_start"),
                        "page_end":      r.get("page_end"),
                        "element_ids":   r.get("element_ids", []),
                        "chunk_type":    r.get("chunk_type"),
                    }
                    for i, r in enumerate(results)
                ]

            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                # 仅对瞬时错误重试：网络/传输错误（RequestError 覆盖 ConnectError/
                # ReadError/TimeoutException）与 5xx；4xx（401/422 等）是确定性
                # 错误，重试无意义，直接给可读错误。
                retryable = isinstance(e, httpx.RequestError) or (
                    getattr(e, "response", None) is not None
                    and e.response.status_code >= 500
                )
                if not retryable:
                    raise KBClientError(f"知识库检索失败: {e}") from e
                last_error = e
                if attempt < max_retries:
                    logger.warning(
                        f"KB retrieval attempt {attempt + 1} failed, retrying... "
                        f"error={e}"
                    )
                    await asyncio.sleep(1.0)
                else:
                    logger.error(f"KB retrieval exhausted retries: {e}")
                    raise KBClientError(
                        f"知识库检索失败（已重试 {max_retries} 次）: {e}",
                    ) from e

        # should be unreachable
        raise KBClientError(f"知识库检索失败: {last_error}")

    async def retrieve_multi(
        self,
        kb_ids: list[str],
        queries: list[str],
        top_k: int = 20,
        rerank_top_k: int = 5,
        score_threshold: float = 0.0,
        user_id: str = "",
        max_retries: int = 2,
    ) -> list[dict]:
        """多路检索：并发检索各 query，合并去重取 top rerank_top_k。"""
        if not queries:
            return []

        # retrieval span 埋点（大盘 P95 用）：有活跃根观测时自动嵌套子 span；
        # 无活跃 trace 时 start_span 是 no-op（yield None）。
        async with get_langfuse().start_span(
            "retrieval", input={"kb_ids": kb_ids, "queries": queries},
        ):
            results = await asyncio.gather(
                *[
                    self._retrieve_once(
                        kb_ids, q, top_k, rerank_top_k, score_threshold, user_id, max_retries,
                    )
                    for q in queries
                ],
                return_exceptions=True,
            )

            rows: list[dict] = []
            first_error: Exception | None = None
            for r in results:
                if isinstance(r, Exception):
                    if first_error is None:
                        first_error = r
                    continue
                rows.extend(r)

            if not rows and first_error is not None:
                raise KBClientError(f"知识库检索失败: {first_error}") from first_error

            return merge_retrieval_results(rows, rerank_top_k)

    async def close(self) -> None:
        await self.client.aclose()
