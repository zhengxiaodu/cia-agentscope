"""知识库大盘主数据客户端 — 数据大盘 C 类取数（只读接口）。

对接 policy-kb-api 的两个大盘只读接口：
  - GET  /api/v1/internal/dashboard/stats          （知识库/已发布/部门主数据）
  - POST /api/v1/internal/dashboard/documents/batch（按 doc_id 批量查文档元数据）
鉴权：Authorization: Bearer <KB_INTERNAL_TOKEN>；响应信封 code==0 为成功。
"""
from __future__ import annotations

import httpx


class KBDashboardQueryError(Exception):
    """知识库大盘主数据查询失败（网络 / 非 200 / 业务 code != 0 / 解析异常）。"""


class KBDashboardClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport=None,
    ):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    async def get_stats(self) -> dict:
        """大盘统计主数据（total_kb_count / published_knowledge / 部门分布等）。"""
        data = await self._request("GET", "/api/v1/internal/dashboard/stats")
        return data or {}

    async def get_documents_batch(self, doc_ids: list[str]) -> list[dict]:
        """按 doc_id 批量查文档元数据（去重、按 1000 分块、合并）。"""
        doc_ids = list(dict.fromkeys(doc_ids))
        if not doc_ids:
            return []
        rows: list[dict] = []
        for i in range(0, len(doc_ids), 1000):
            chunk = doc_ids[i : i + 1000]
            data = await self._request(
                "POST", "/api/v1/internal/dashboard/documents/batch", json={"doc_ids": chunk},
            )
            rows.extend(data or [])
        return rows

    async def _request(self, method: str, path: str, *, json: dict | None = None):
        headers = {"Authorization": f"Bearer {self._token}"}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                resp = await client.request(method, self._base + path, headers=headers, json=json)
            except httpx.HTTPError as exc:
                raise KBDashboardQueryError(f"知识库大盘接口请求失败: {exc}") from exc
            if resp.status_code != 200:
                raise KBDashboardQueryError(f"知识库大盘接口返回 {resp.status_code}: {resp.text[:200]}")
            try:
                body = resp.json()
            except ValueError as exc:
                raise KBDashboardQueryError(f"知识库大盘接口返回非 JSON: {resp.text[:200]}") from exc
            if body.get("code", -1) != 0:
                raise KBDashboardQueryError(
                    f"知识库大盘接口业务错误 {body.get('code')}: {body.get('message', 'unknown')}"
                )
            return body.get("data")
