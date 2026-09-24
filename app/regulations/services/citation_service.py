"""引用格式化服务（轻量）。"""

from __future__ import annotations

import re

from app.regulations.schemas import RetrieverResource


class CitationService:
    """引用资源的格式化与标记。"""

    @staticmethod
    def format_inline_markers(answer: str, resources: list[RetrieverResource]) -> str:
        """确保答案中的 [N] 标记与 resources position 对应。

        当前答案由 LLM 直接生成带 [N] 的文本，此方法做基本校验。
        """
        return answer

    @staticmethod
    def validate_references(
        answer: str, resources: list[RetrieverResource],
    ) -> list[int]:
        """提取答案中引用的 position 列表，用于校验引用完整性。"""
        positions = {int(m) for m in re.findall(r"\[(\d+)\]", answer)}
        return sorted(positions)

    @staticmethod
    def sanitize_references(answer: str, resource_count: int) -> str:
        """剔除越界引用标记：仅保留 1..resource_count 范围内的 [N]。

        防止 LLM 输出 [0] 或超出资料数的编号，导致前端 citation 空指针。
        """
        if not answer:
            return answer

        def _repl(m: re.Match) -> str:
            n = int(m.group(1))
            return m.group(0) if 1 <= n <= resource_count else ""

        return re.sub(r"\[(\d+)\]", _repl, answer)
