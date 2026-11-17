"""文件变更检测：对比前后快照，计算本轮新生成的文件。"""

import os

# 生成文件返回白名单：常规文档/表格/演示/图片及指定代码类型会下发前端并落库，
# svg/log/json 等过程文件被屏蔽，不进入 files_generated 事件与 session_files
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    # 文档
    "doc", "docx", "pdf", "md", "txt",
    # 表格
    "xls", "xlsx", "csv",
    # 演示
    "ppt", "pptx",
    # 图片
    "png", "jpg", "jpeg","vue","sql",
    # 网页与代码
    "html", "css", "js", "py","json","yml","yaml","sh",
})


def is_allowed_file(rel_path: str) -> bool:
    """判断扩展名（大小写不敏感）是否在返回白名单内；无扩展名视为不允许。"""
    _, ext = os.path.splitext(rel_path)
    return ext.lstrip(".").lower() in ALLOWED_EXTENSIONS


def filter_allowed(paths: list[str]) -> list[str]:
    """过滤路径列表，仅保留白名单扩展名的文件，保持原顺序。"""
    return [p for p in paths if is_allowed_file(p)]


def diff(
    before: set[str] | dict[str, tuple[str, str]],
    after: set[str] | dict[str, tuple[str, str]],
    exclude_prefixes: tuple[str, ...] = (),
) -> list[str]:
    """返回本轮"新增或内容变化"的相对路径列表，按路径字符串升序排序。

    快照支持两种形态：
    - set[str]：纯路径集合，只识别新增路径（旧行为）
    - dict[str, (mtime, size)]：带修改签名，同路径文件 mtime/size
      变化（重生成/覆盖）也算本轮产物

    exclude_prefixes：排除的相对路径前缀（平台写入目录，非模型产物）。
    如 uploads/——上传文件解析内容由 persist_upload_files_to_workspace
    在 before 快照之后写入沙箱，不排除会被误判为"本轮生成的文件"。
    """
    if isinstance(before, dict) and isinstance(after, dict):
        added = {p for p, sig in after.items() if before.get(p) != sig}
    else:
        # 纯路径集合（或混用形态）：退化为路径差集
        before_paths = set(before)
        added = set(after) - before_paths
    if exclude_prefixes:
        added = {p for p in added if not p.startswith(exclude_prefixes)}
    return sorted(added)
