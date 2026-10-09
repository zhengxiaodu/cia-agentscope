"""Langfuse 查询客户端 — 数据大盘 A/A⁺/B 类取数（v1 REST，httpx）。

自建 Langfuse 3.175.0 无 v2 API（/api/public/v2/* 仅 Langfuse Cloud），
故直连 v1 行级端点：
  - GET /api/public/traces        （name=kb_search，分页）
  - GET /api/public/observations  （name=retrieval，取 latency）
鉴权：Authorization: Basic base64(public_key:secret_key)。
"""
from __future__ import annotations

import base64
import datetime as dt
from typing import Any

import httpx


class LangfuseQueryError(Exception):
    """Langfuse 查询失败（网络 / 非 200 / 解析异常）。"""


class LangfuseQueryClient:
    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        timeout: float = 10.0,
        page_size: int = 100,
        transport=None,
    ):
        self._base = host.rstrip("/")
        self._auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._timeout = timeout
        self._page_size = page_size
        self._transport = transport

    async def list_traces(self, from_dt: dt.datetime, to_dt: dt.datetime) -> list[dict]:
        """拉取窗口内全部 kb_search trace（分页）。"""
        return await self._paginate(
            "/api/public/traces",
            params={
                "name": "kb_search",
                "fromTimestamp": from_dt.isoformat(),
                "toTimestamp": to_dt.isoformat(),
            },
        )

    async def list_retrieval_latencies(self, from_dt: dt.datetime, to_dt: dt.datetime) -> list[dict]:
        """拉取窗口内 retrieval span 的 {trace_id, latency} 列表（秒）。

        返回 trace_id 供调用方按「父 trace 是否 kb_search」过滤，避免 P95 混入
        非 kb_search 旧 trace 的 retrieval span（口径与 valid_search_count 不一致）。
        """
        obs = await self._paginate(
            "/api/public/observations",
            params={
                "name": "retrieval",
                "fromStartTime": from_dt.isoformat(),
                "toStartTime": to_dt.isoformat(),
            },
        )
        return [
            {"trace_id": o.get("traceId"), "latency": float(o["latency"])}
            for o in obs
            if o.get("latency") is not None
        ]

    async def _paginate(self, path: str, params: dict[str, Any]) -> list[dict]:
        headers = {"Authorization": f"Basic {self._auth}"}
        all_rows: list[dict] = []
        page = 1
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            while True:
                qp = {**params, "page": page, "limit": self._page_size}
                try:
                    resp = await client.get(self._base + path, params=qp, headers=headers)
                except httpx.HTTPError as exc:
                    raise LangfuseQueryError(f"Langfuse 请求失败: {exc}") from exc
                if resp.status_code != 200:
                    raise LangfuseQueryError(f"Langfuse 返回 {resp.status_code}: {resp.text[:200]}")
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise LangfuseQueryError(f"Langfuse 返回非 JSON: {resp.text[:200]}") from exc
                rows = body.get("data") or []
                all_rows.extend(rows)
                meta = body.get("meta") or {}
                total_pages = int(meta.get("totalPages") or 1)
                if page >= total_pages or not rows:
                    break
                page += 1
        return all_rows
