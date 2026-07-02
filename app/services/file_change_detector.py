import logging
import mimetypes
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 顶层要跳过的子目录名（/upload 落点）
_EXCLUDED_TOP_DIRS = {"data", "skills"}


def snapshot(workdir: str) -> set[str]:
    """递归扫描 workdir，返回相对路径集合（POSIX 风格 / 分隔）。

    - 跳过顶层名为 `data` 和 `skills` 的子目录（/upload 落点 / 技能目录）
    - 跳过所有层级中文件名为 `.mcp` 的文件
    - 目录不存在返回空集合，不抛异常
    - 仅收录文件（不含目录本身）
    - 相对路径形如 "report.csv" / "sub/dir/chart.png"
    """
    if not workdir or not os.path.isdir(workdir):
        return set()

    result: set[str] = set()
    for root, dirs, files in os.walk(workdir):
        # 仅在顶层（root == workdir）剪掉 _EXCLUDED_TOP_DIRS 中的子目录
        if os.path.abspath(root) == os.path.abspath(workdir):
            dirs[:] = [d for d in dirs if d not in _EXCLUDED_TOP_DIRS]
        rel = os.path.relpath(root, workdir)
        prefix = "" if rel == "." else rel.replace(os.sep, "/")
        for filename in files:
            if filename == ".mcp":
                continue
            rel_path = f"{prefix}/{filename}" if prefix else filename
            result.add(rel_path)
    return result


def diff(before: set[str], after: set[str]) -> list[str]:
    """返回 after - before 的新相对路径列表，按路径字符串升序排序。"""
    return sorted(after - before)


def build_file_meta(workdir: str, rel_path: str, session_id: str) -> dict | None:
    """构造单个文件的元信息 dict。

    Returns:
        {
            "name": basename,
            "path": rel_path,
            "url": f"/files/{session_id}/{rel_path}",
            "size": 文件字节数,
            "media_type": mimetypes.guess_type(rel_path)[0] or "application/octet-stream",
        }
        文件不存在返回 None
    """
    abs_path = os.path.join(workdir, rel_path)
    if not os.path.isfile(abs_path):
        return None
    size = os.path.getsize(abs_path)
    name = os.path.basename(rel_path)
    media_type = mimetypes.guess_type(rel_path)[0] or "application/octet-stream"
    url = f"/files/{session_id}/{rel_path}"
    return {
        "name": name,
        "path": rel_path,
        "url": url,
        "size": size,
        "media_type": media_type,
    }
