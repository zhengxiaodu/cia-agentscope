"""制度问答路由 — POST /api/v1/policy/qa（blocking/streaming，无状态，不落库）
与用户可见知识库文档两个接口（列文档 / 查看源文档）。

- /api/v1/policy/qa 响应保持原制度问答后端契约（answer/citations/
  app_serial_number/model/created_at 直接在顶层，SSE 事件同 /chat-messages），
  不套本项目 success_response 包装。
- 文档接口面向本项目前端，用统一信封 {code, msg, data}；kb_ids 一律由后端
  按 Redis 用户权限解析（前端不传），可传 knowledge_bases 联动前端选择器过滤。
出错时以 HTTPException 返回（本项目无全局 AppError handler，路由内局部处理）。
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.dependencies import current_user
from app.regulations.schemas import GeneralQARequest, GeneralQAResponse
from app.regulations.services.kb_client import KBClientError
from app.services.auth_service import get_user_permissions
from tools.policy_qa_tools import resolve_kb_ids

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/v1/policy/qa", response_model=None)
async def policy_qa(
    request: Request,
    body: GeneralQARequest,
    user: dict = Depends(current_user),
) -> GeneralQAResponse | StreamingResponse:
    """通用问答：传入问题 + 知识库列表，返回回答正文与引用文档。

    - response_mode=blocking（默认）：阻塞返回 JSON（GeneralQAResponse）。
    - response_mode=streaming：返回 SSE 流（对齐 /chat-messages 事件契约）。
    - 无状态：不建会话、不落库，出口打一条 [policy-qa] 结果日志（只记概要，
      不记正文；正文与引用写 Langfuse trace 供溯源）。
    - kb_ids 与 Redis 中该用户的制度问答权限取交集（越权库 ID 静默剔除，
      交集为空返回 403）；登录态过期返回 401。
    - KBClientError → 502，其余异常 → 500。
    """
    svc = getattr(request.app.state, "policy_qa_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="制度问答服务未就绪")

    user_id = str(user.get("user_id"))
    user_department = user.get("department") or ""

    # 越权防护：请求的 kb_ids 必须落在用户权限可访问范围内
    permissions = await _load_user_permissions(request, user)
    allowed_ids = set(resolve_kb_ids(permissions))
    kb_ids = list(dict.fromkeys(
        k for k in body.knowledge_base_ids if k in allowed_ids
    ))
    if not kb_ids:
        raise HTTPException(status_code=403, detail="无权访问所请求的知识库")
    logger.info(
        f"[policy-qa] kb_ids 权限校验通过 user={user_id} "
        f"requested={len(body.knowledge_base_ids)} allowed={len(kb_ids)}"
    )

    try:
        if body.response_mode == "streaming":
            return await svc.run_general_qa_stream(
                question=body.question,
                kb_ids=kb_ids,
                serial=body.app_serial_number,
                user_id=user_id,
                user_department=user_department,
                enable_query_understanding=body.enable_query_understanding,
            )
        return await svc.run_general_qa(
            question=body.question,
            kb_ids=kb_ids,
            serial=body.app_serial_number,
            user_id=user_id,
            user_department=user_department,
            enable_query_understanding=body.enable_query_understanding,
        )
    except KBClientError as e:
        logger.error(f"[policy-qa] 知识库检索失败 user={user_id}: {e}")
        raise HTTPException(status_code=502, detail=f"知识库检索失败: {e}") from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[policy-qa] 内部错误 user={user_id}")
        raise HTTPException(status_code=500, detail=f"内部服务错误: {e}") from e


# ══════════════════════════════════════════════════════════
# 用户可见知识库文档接口（契约见 policy-qa-visible-documents.md）
# ══════════════════════════════════════════════════════════

class DocumentQueryRequest(BaseModel):
    """列文档请求：kb_ids 由后端按用户权限解析，前端不传。"""
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)  # kb_app 契约上限 100
    search: str = ""
    # 与 /chat 知识库选择器联动：空 = 全选（权限内全部知识库）
    knowledge_bases: List[str] = []
    category_id: str | None = None


async def _load_user_permissions(request: Request, user: dict) -> dict:
    """取当前用户 Redis 缓存权限；登录态过期抛 401，Redis 未就绪抛 500。"""
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis 未就绪")
    user_id = str(user.get("user_id"))
    perms_data = await get_user_permissions(redis_client, user_id)
    if not perms_data:
        raise HTTPException(status_code=401, detail="用户登录态已过期，请重新登录")
    return perms_data.get("permissions") or {}


def _get_kb_client(request: Request):
    kb_client = getattr(request.app.state, "regulations_kb_client", None)
    if kb_client is None:
        raise HTTPException(status_code=503, detail="知识库服务未就绪")
    return kb_client


@router.post("/api/v1/policy/documents/query")
async def policy_documents_query(
    request: Request,
    body: DocumentQueryRequest,
    user: dict = Depends(current_user),
):
    """列用户可见知识库文档（含摘要 / 示例问题，供问答界面文档列表）。

    kb_ids 由后端按 Redis 权限解析（前端不传）；knowledge_bases 非空时
    与权限映射取交集（前端选择器联动）。交集为空直接返回空列表，不调 kb_app。
    响应 data 字段与 kb_app documents/query 契约一致（items/total/page/page_size）。
    """
    permissions = await _load_user_permissions(request, user)
    kb_ids = resolve_kb_ids(permissions, body.knowledge_bases)
    if not kb_ids:
        logger.info(
            f"[policy-qa] 列文档权限为空 user={user.get('user_id')} "
            f"knowledge_bases={body.knowledge_bases}"
        )
        return {
            "code": 200,
            "msg": "success",
            "data": {
                "items": [],
                "total": 0,
                "page": body.page,
                "page_size": body.page_size,
            },
        }

    kb_client = _get_kb_client(request)
    try:
        data = await kb_client.query_documents(
            kb_ids,
            page=body.page,
            page_size=body.page_size,
            search=body.search,
            category_id=body.category_id,
        )
    except KBClientError as e:
        logger.error(f"[policy-qa] 文档列表查询失败 user={user.get('user_id')}: {e}")
        raise HTTPException(status_code=502, detail=f"文档列表查询失败: {e}") from e
    return {"code": 200, "msg": "success", "data": data}


async def _verify_doc_in_scope(kb_client, kb_ids: list[str], doc_id: str) -> bool:
    """严格校验 doc_id 归属：分页遍历用户权限内文档，命中 doc_id 才放行。

    文档量级百级、内网分页调用，每页 100；kb_app 排序稳定（kb_id 升序 →
    updated_at 降序），跨页不重复。后续文档量大时可加 doc_id→kb_id TTL 缓存。
    """
    if not kb_ids:
        return False
    page, page_size = 1, 100
    while True:
        data = await kb_client.query_documents(
            kb_ids, page=page, page_size=page_size,
        )
        for kb in data.get("items") or []:
            for cat in kb.get("categories") or []:
                if any(doc.get("doc_id") == doc_id for doc in cat.get("items") or []):
                    return True
        total = data.get("total") or 0
        if page * page_size >= total:
            return False
        page += 1


@router.get("/api/v1/policy/documents/{doc_id}/file")
async def policy_document_file(
    doc_id: str,
    request: Request,
    user: dict = Depends(current_user),
):
    """查看源文档：校验 doc_id 在用户权限内文档中后，代理转发原始文件流。

    - 权限解析取用户全量 kb_ids（不受前端知识库选择过滤：列表展示过的都允许看）
    - 未命中 → 404（文案统一"文档不存在或您无权查看"，不暴露差异信号）
    - 200 时透传 kb_app 的 Content-Type / Content-Disposition / X-Page-Count
    """
    permissions = await _load_user_permissions(request, user)
    kb_client = _get_kb_client(request)
    kb_ids = resolve_kb_ids(permissions)
    try:
        in_scope = await _verify_doc_in_scope(kb_client, kb_ids, doc_id)
    except KBClientError as e:
        logger.error(f"[policy-qa] 源文档归属校验失败 user={user.get('user_id')}: {e}")
        raise HTTPException(status_code=502, detail=f"源文档校验失败: {e}") from e
    if not in_scope:
        raise HTTPException(status_code=404, detail="文档不存在或您无权查看")

    try:
        resp = await kb_client.get_document_file(doc_id)
    except KBClientError as e:
        logger.error(f"[policy-qa] 源文档获取失败 user={user.get('user_id')}: {e}")
        raise HTTPException(status_code=502, detail=f"源文档获取失败: {e}") from e
    if resp.status_code != 200:
        # kb_app 404（不存在 / 非 active / 原始文件未入 MinIO）统一转 404 占位
        logger.warning(
            f"[policy-qa] 源文档非 200 status={resp.status_code} doc={doc_id}"
        )
        raise HTTPException(status_code=404, detail="文档不存在或您无权查看")

    passthrough_headers = {
        h: resp.headers[h]
        for h in ("content-disposition", "x-page-count")
        if h in resp.headers
    }
    media_type = resp.headers.get("content-type", "application/octet-stream")
    return Response(content=resp.content, media_type=media_type, headers=passthrough_headers)
