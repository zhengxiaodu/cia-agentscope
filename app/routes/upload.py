import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request

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
        request, session_id, filename, media_type, content
    )

    return UploadResponse(
        code=200,
        msg="success",
        session_id=session_id,
        data={
            "datablock": {
                "id": uuid.uuid4().hex,
                "name": filename,
                "source": {
                    "url": f"uploaded://{filename}",
                    "media_type": media_type,
                },
            },
            "file_id": file_id,
        },
    )
