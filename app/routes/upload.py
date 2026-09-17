import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, Request

from app.dependencies import current_user
from app.services.file_service import FileService
from app.config import UPLOAD_MAX_SIZE_MB, UPLOAD_ALLOWED_MEDIA_TYPES
from app.models.upload import UploadResponse, UploadErrorResponse
from app.services.file_parse_service import start_background_parse

router = APIRouter()


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    session_id: str = Form(None),
    user: dict = Depends(current_user),
    request: Request = None,
):
    """上传文件：插入 upload_files 记录并启动后台解析，立即返回。

    解析策略（见 file_parse_service.classify_parse_type）：
    - 图片/文档/pdf/表格 → MinerU；音频 → 音频转写模型；纯文本直接读取
    - 不再写入沙箱/宿主机工作区：问答时解析内容经提示词注入给 agent
    """
    # Validate file size
    content = await file.read()
    if not FileService.validate_file_size(content, UPLOAD_MAX_SIZE_MB):
        raise HTTPException(
            status_code=413,
            detail=UploadErrorResponse(
                code=413,
                msg=f"文件大小超过限制（最大 {UPLOAD_MAX_SIZE_MB}MB）",
            ).model_dump(),
        )

    # Validate media type
    media_type = file.content_type or "application/octet-stream"
    if not FileService.validate_media_type(media_type, UPLOAD_ALLOWED_MEDIA_TYPES):
        raise HTTPException(
            status_code=415,
            detail=UploadErrorResponse(
                code=415,
                msg="不支持的文件类型",
            ).model_dump(),
        )

    user_id = user.get("user_id")
    if not session_id:
        session_service = request.app.state.session_service
        session_id = await session_service.get_or_create_session(None, user_id)

    # 后台异步解析：立即返回 file_id，解析结果稍后写入 upload_files 表
    filename = file.filename or "unknown"
    file_id = await start_background_parse(
        request, session_id, user_id, filename, media_type, content
    )

    return UploadResponse(
        code=200,
        msg="success",
        data={
            "datablock": {
                "id": uuid.uuid4().hex,
                "name": filename,
                "source": {
                    "url": f"uploaded://{filename}",
                    "media_type": media_type,
                    "size": len(content)
                },
            },
            "file_id": file_id,
            "session_id": session_id
        },
    )


@router.get("/uploads")
async def list_user_uploads(
    request: Request,
    user: dict = Depends(current_user),
):
    """按 user_id 查询该用户上传过的全部文件（最新在前）。

    每条记录返回 upload_file_id / session_id / message_id / filename /
    media_type / file_size；message_id 为 null 表示该文件尚未被对话消费。
    """
    dao = getattr(request.app.state, "upload_file_dao", None)
    user_id = user.get("user_id")
    if dao is None:
        raise HTTPException(status_code=500, detail="upload_file_dao 未初始化")

    files = await dao.list_files_by_user(user_id)
    return {"code": 200, "msg": "success", "data": {"files": files}}


@router.delete("/uploads")
async def delete_user_uploads(
    request: Request,
    upload_file_id: Optional[int] = Query(
        None, description="要删除的上传文件 id；不传则删除该用户全部上传记录"
    ),
    user: dict = Depends(current_user),
):
    """删除上传文件记录。

    传 upload_file_id 时删除对应单条记录（不存在或不属于该用户返回 404）；
    不传时删除该用户的全部上传记录。仅删 DB 行（现架构上传文件不落盘，
    解析内容在库内，无物理文件需清理）。
    """
    dao = getattr(request.app.state, "upload_file_dao", None)
    user_id = user.get("user_id")
    if dao is None:
        raise HTTPException(status_code=500, detail="upload_file_dao 未初始化")

    if upload_file_id is not None:
        ok = await dao.delete_by_id(user_id, upload_file_id)
        if not ok:
            return {"code": 404, "msg": "文件不存在", "data": {}}
        return {"code": 200, "msg": "success", "data": {"deleted": 1}}

    deleted = await dao.delete_all_by_user(user_id)
    return {"code": 200, "msg": "success", "data": {"deleted": deleted}}
