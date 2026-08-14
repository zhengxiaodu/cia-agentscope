import logging
import mimetypes
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app.config import WORKSPACE_BASEDIR
from app.dependencies import current_user
from app.services.opensandbox_workspace_manager import OpenSandboxWorkspaceManager

logger = logging.getLogger(__name__)
router = APIRouter()


def _build_file_response(
    content: bytes, rel: str, media_type: str, mode: str
) -> Response:
    """根据 mode 构造下载 / 内联预览响应。

    mode != inline：附件下载。
    mode == inline：文本类返回 text/...; charset=utf-8，图片类返回 image mime；
    pdf/docx 等不支持预览的类型返回 415。
    """
    basename = os.path.basename(rel) or rel
    if mode != "inline":
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{basename}"'},
        )
    is_text = media_type.startswith("text/") or OpenSandboxWorkspaceManager._is_text_rel(rel)
    is_image = media_type.startswith("image/")
    if is_text:
        ct = media_type if "charset=" in media_type else f"{media_type}; charset=utf-8"
        return Response(
            content=content,
            media_type=ct,
            headers={"Content-Disposition": "inline"},
        )
    if is_image:
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": "inline"},
        )
    raise HTTPException(status_code=415, detail="暂不支持预览，请下载")


@router.get("/files/{session_id}/{path:path}")
async def download_session_file(
    request: Request,
    session_id: str,
    path: str,
    mode: str = "download",
    user: dict = Depends(current_user),
):
    """下载或内联预览指定 session 工作目录下的文件。

    鉴权：依赖 current_user（未登录 401 由依赖抛出）。
    后端分支：
    - opensandbox：通过 workspace_manager.read_session_file 从沙箱读取
    - docker：原宿主机 FileResponse 路径
    越权校验：规范化相对路径，禁止 .. / 绝对路径，逃逸返回 403。
    mode=download 返回附件下载；mode=inline 返回内联预览（文本/图片），
    不支持预览的类型返回 415。
    """
    try:
        workspace_manager = getattr(request.app.state, "workspace_manager", None)
        workspace_backend = getattr(request.app.state, "workspace_backend", "docker")
        user_id = user.get("user_id", "")

        # 越权校验：规范化相对路径，禁止 .. 和绝对路径
        rel = path.replace("\\", "/").lstrip("/")
        if os.path.isabs(path) or rel.startswith("..") or "/.." in rel or rel == "..":
            raise HTTPException(status_code=403, detail="无权访问该路径")

        media_type = mimetypes.guess_type(rel)[0] or "application/octet-stream"

        if workspace_backend == "opensandbox" and workspace_manager is not None:
            content = await workspace_manager.read_session_file(user_id, session_id, rel)
            if content is None:
                raise HTTPException(status_code=404, detail="文件不存在")
            return _build_file_response(content, rel, media_type, mode)

        # Docker 后端：原宿主机 FileResponse 逻辑
        base = os.path.realpath(os.path.join(WORKSPACE_BASEDIR, session_id))
        target = os.path.realpath(os.path.join(base, path))
        if target != base and not target.startswith(base + os.sep):
            raise HTTPException(status_code=403, detail="无权访问该路径")
        if not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="文件不存在")
        if mode == "inline":
            with open(target, "rb") as f:
                content = f.read()
            return _build_file_response(content, rel, media_type, mode)
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
