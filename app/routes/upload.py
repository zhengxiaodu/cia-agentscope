import os

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request

from app.dependencies import current_user
from app.services.file_service import FileService
from app.config import UPLOAD_MAX_SIZE_MB, UPLOAD_ALLOWED_MEDIA_TYPES, WORKSPACE_BASEDIR
from app.models.upload import UploadResponse, UploadErrorResponse

router = APIRouter()


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    session_id: str = Form(None),
    user: dict = Depends(current_user),
    request: Request = None,
):
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

    # Save file and return DataBlock
    user_id = user.get("user_id")
    if not session_id:
        session_service = request.app.state.session_service
        session_id = await session_service.get_or_create_session(None, user_id)
    workdir = os.path.join(WORKSPACE_BASEDIR, session_id)
    file_service = FileService(workdir=workdir)
    datablock = await file_service.save_upload(
        session_id=session_id,
        filename=file.filename or "unknown",
        content=content,
        media_type=media_type,
    )

    return UploadResponse(
        code=200,
        msg="success",
        data={"datablock": datablock.model_dump()},
    )