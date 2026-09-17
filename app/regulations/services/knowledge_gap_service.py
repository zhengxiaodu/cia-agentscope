"""知识缺口服务 — 缺口工单的生命周期（写入 / 读 open / 标记 resolved）。

缺口是「有生命周期」的实体，区别于 Langfuse 里不可变的空应答事件：
- record_gap：空应答发生时按 (kb_id, 规范化 question) upsert open 缺口；
- load_open_gaps_grouped：大盘 knowledge_gaps 读 open 缺口按 kb 分组；
- resolve_gaps：补充文档后标记 resolved，已闭合缺口不再展示。

自 qa_app/services/knowledge_gap_service.py 移植：sqlalchemy ORM 替换为
KnowledgeGapDAO（aiomysql 直写），模块级函数改为类（model_cfg 运行时注入）。
"""
from __future__ import annotations

import hashlib
import logging

from app.regulations.dao.knowledge_gap_dao import KnowledgeGapDAO
from app.regulations.providers.llm import get_rewrite_llm

logger = logging.getLogger(__name__)


def _normalize(question: str) -> str:
    """折叠空白，用于去重哈希。"""
    return " ".join((question or "").strip().split())


def _question_hash(kb_id: str, question: str) -> str:
    return hashlib.sha256(f"{kb_id}\x00{_normalize(question)}".encode("utf-8")).hexdigest()


_SUMMARIZE_PROMPT = (
    "你是知识缺口主题归纳助手。用不超过 6 个字归纳下面这个用户问题所属的宽泛主题，"
    "合并相近问题到同一主题（如输出「差旅报销」而非「差旅报销流程标准」）。"
    "只输出主题词本身，不要解释、不要标点、不要引号。\n用户问题：{question}"
)


class KnowledgeGapService:
    """知识缺口服务（写入 / 读 open / 标记 resolved）。"""

    def __init__(self, dao: KnowledgeGapDAO, model_cfg: dict):
        self.dao = dao
        self.model_cfg = model_cfg

    async def record_gap(self, kb_id: str, question: str) -> None:
        """后台任务入口：空应答时 upsert 一条 open 缺口（异常不影响主流程）。

        仅当缺口**新建**（非同问增量）时，LLM 概括主题写回 question_type；
        后续读路径直接按 question_type 分组，不再触发 LLM。
        """
        if not kb_id or not question:
            return
        h = _question_hash(kb_id, question)
        is_new = False
        try:
            is_new = await self.dao.upsert_open_gap(kb_id, question, h)
        except Exception as exc:  # noqa: BLE001 —— 后台落缺口失败不打断主流程
            logger.error(f"record_gap failed kb={kb_id}: {exc}")
            return

        if not is_new:
            return

        # 新缺口：LLM 概括主题并写回（失败仅告警，缺口仍以「未分类」展示）
        qtype = await self.summarize_question_type(question)
        if not qtype:
            return
        try:
            await self.dao.update_question_type(kb_id, h, qtype)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"record_gap update question_type failed kb={kb_id}: {exc}")

    async def load_open_gaps_grouped(self) -> list[dict]:
        """读 open 缺口按 (kb_id, question_type) 分组，按空应答计数降序。

        question_type 为空（LLM 尚未总结/失败）时 DAO 兜底为「未分类」。
        """
        return await self.dao.list_open_gaps_grouped()

    async def resolve_gaps(
        self,
        gap_ids: list[str] | None = None,
        kb_id: str | None = None,
    ) -> int:
        """标记 open 缺口为 resolved（按 gap_ids 或 kb_id 批量）。返回影响行数。"""
        return await self.dao.resolve_gaps(gap_ids=gap_ids, kb_id=kb_id)

    async def summarize_question_type(self, question: str) -> str:
        """用 LLM 把空检索问题概括成 ≤10 字主题短语；失败/空返回空串。

        只在缺口新建时后台调用一次（不阻塞问答响应）；读路径不再触发 LLM。
        """
        question = (question or "").strip()
        if not question:
            return ""
        try:
            msg = await get_rewrite_llm(self.model_cfg).ainvoke(
                _SUMMARIZE_PROMPT.format(question=question)
            )
            text = (getattr(msg, "content", "") or "").strip()
            return text.strip("\"'“”‘’ \n")[:64]
        except Exception as exc:  # noqa: BLE001 —— 概括失败不影响主流程
            logger.warning(f"summarize_question_type failed for {question!r}: {exc}")
            return ""
