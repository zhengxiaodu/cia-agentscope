"""文件变更检测：对比前后快照，计算本轮新生成的文件。"""


def diff(before: set[str], after: set[str]) -> list[str]:
    """返回 after - before 的新相对路径列表，按路径字符串升序排序。"""
    return sorted(after - before)
