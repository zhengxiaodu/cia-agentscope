import logging
import mimetypes
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.config import WORKSPACE_BASEDIR
from app.dependencies import current_user

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/files/{session_id}/{path:path}")
async def download_session_file(
    session_id: str,
    path: str,
    user: dict = Depends(current_user),
):
    """下载指定 session 工作目录下的文件。

    鉴权：依赖 current_user（未登录 401 由依赖抛出）。
    越权校验：realpath 后必须仍在 {WORKSPACE_BASEDIR}/{session_id} 之内。
    """
    try:
        base = os.path.realpath(os.path.join(WORKSPACE_BASEDIR, session_id))
        target = os.path.realpath(os.path.join(base, path))

        # 越权校验：target 必须等于 base 或位于 base 之下
        if target != base and not target.startswith(base + os.sep):
            raise HTTPException(status_code=403, detail="无权访问该路径")

        # 存在性校验
        if not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="文件不存在")

        media_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        return FileResponse(
            target,
            filename=os.path.basename(target),
            media_type=media_type,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("下载文件失败: %s", e)
        raise HTTPException(status_code=500, detail="服务器内部错误")
