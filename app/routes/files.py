import logging
import mimetypes
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app.dependencies import current_user
from app.services.opensandbox_workspace_manager import OpenSandboxWorkspaceManager

logger = logging.getLogger(__name__)
router = APIRouter()


def _content_disposition(basename: str) -> str:
    """构造 attachment 的 Content-Disposition 头（RFC 5987/6266）。

    HTTP 头只支持 latin-1，中文等非 ASCII 文件名直接拼接会导致
    'latin-1' codec 编码错误；改为 ASCII 回退名 + filename*=UTF-8'' 双写：
    现代浏览器优先读 filename* 显示原名，老客户端回退到 ASCII 名。
    """
    ascii_name = basename.encode("latin-1", "ignore").decode("latin-1") or "download"
    return (
        f"attachment; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{quote(basename)}"
    )


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
            headers={"Content-Disposition": _content_disposition(basename)},
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
    通过 workspace_manager.read_session_file 从 OpenSandbox 沙箱读取。
    越权校验：规范化相对路径，禁止 .. / 绝对路径，逃逸返回 403。
    mode=download 返回附件下载；mode=inline 返回内联预览（文本/图片），
    不支持预览的类型返回 415。
    """
    try:
        workspace_manager = getattr(request.app.state, "workspace_manager", None)
        user_id = user.get("user_id", "")

        # 越权校验：规范化相对路径，禁止 .. 和绝对路径
        rel = path.replace("\\", "/").lstrip("/")
        if os.path.isabs(path) or rel.startswith("..") or "/.." in rel or rel == "..":
            raise HTTPException(status_code=403, detail="无权访问该路径")

        if workspace_manager is None:
            raise HTTPException(status_code=503, detail="工作区服务不可用")

        media_type = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        content = await workspace_manager.read_session_file(user_id, session_id, rel)
        if content is None:
            raise HTTPException(status_code=404, detail="文件不存在")
        return _build_file_response(content, rel, media_type, mode)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("下载文件失败: %s", e)
        raise HTTPException(status_code=500, detail="服务器内部错误")
