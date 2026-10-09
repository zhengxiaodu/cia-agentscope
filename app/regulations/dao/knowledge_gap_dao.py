"""制度问答知识缺口 DAO。

knowledge_gaps 表记录各知识库的空应答问题：
- 命中空应答时 upsert（已有 open 记录则累加 empty_count，否则新建）
- 后台 LLM 概括回填 question_type
- 按 kb_id + question_type 汇总 open 缺口，补库后批量置 resolved
- question_hash 由 service 层计算，DAO 只接收哈希值
"""
import logging
from uuid import uuid4

import aiomysql

logger = logging.getLogger(__name__)


class KnowledgeGapDAO:
    """知识缺口数据访问层"""

    def __init__(self, pool: aiomysql.Pool):
        self.pool = pool

    async def upsert_open_gap(
        self, kb_id: str, question: str, question_hash: str
    ) -> bool:
        """记录一次空应答。

        同一 kb_id + question_hash 已有 open 记录时累加 empty_count
        并刷新 last_seen_at，返回 False；否则新建 open 记录
        （gap_id 为 uuid4().hex，empty_count=1，
        first_seen_at/last_seen_at/created_at=NOW()），返回 True。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await conn.begin()
                try:
                    await cur.execute(
                        "SELECT id FROM knowledge_gaps "
                        "WHERE status = 'open' AND kb_id = %s "
                        "AND question_hash = %s LIMIT 1",
                        (kb_id, question_hash),
                    )
                    row = await cur.fetchone()
                    if row is not None:
                        await cur.execute(
                            "UPDATE knowledge_gaps "
                            "SET empty_count = empty_count + 1, "
                            "last_seen_at = NOW() "
                            "WHERE id = %s",
                            (row["id"],),
                        )
                        await conn.commit()
                        return False
                    await cur.execute(
                        "INSERT INTO knowledge_gaps "
                        "(gap_id, kb_id, question, question_hash, "
                        "status, empty_count, first_seen_at, last_seen_at, "
                        "created_at) "
                        "VALUES (%s, %s, %s, %s, 'open', 1, "
                        "NOW(), NOW(), NOW())",
                        (uuid4().hex, kb_id, question, question_hash),
                    )
                    await conn.commit()
                    return True
                except Exception:
                    await conn.rollback()
                    raise

    async def update_question_type(
        self, kb_id: str, question_hash: str, question_type: str
    ) -> None:
        """回填 open 记录的问题主题概括（LLM 后台总结）。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE knowledge_gaps "
                    "SET question_type = %s, updated_at = NOW() "
                    "WHERE status = 'open' AND kb_id = %s "
                    "AND question_hash = %s",
                    (question_type, kb_id, question_hash),
                )
                await conn.commit()

    async def list_open_gaps_grouped(self) -> list[dict]:
        """按 kb_id + question_type 汇总 open 缺口的空应答次数（降序）。

        question_type 为 NULL 的归入"未分类"。
        返回 [{"kb_id", "question_type", "empty_answer_count"}]。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT kb_id, "
                    "IFNULL(question_type, '未分类') AS question_type, "
                    "SUM(empty_count) AS cnt "
                    "FROM knowledge_gaps WHERE status = 'open' "
                    "GROUP BY kb_id, question_type "
                    "ORDER BY cnt DESC"
                )
                rows = await cur.fetchall()
                await conn.commit()
                return [
                    {
                        "kb_id": row["kb_id"],
                        "question_type": row["question_type"],
                        "empty_answer_count": row["cnt"],
                    }
                    for row in rows
                ]

    async def resolve_gaps(
        self,
        gap_ids: list[str] | None = None,
        kb_id: str | None = None,
    ) -> int:
        """把 open 缺口置为 resolved，返回影响行数。

        gap_ids 提供时按 gap_id IN (...) 批量关闭；否则按 kb_id
        关闭该库全部 open 缺口；两者均未提供时不做任何操作。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                if gap_ids:
                    placeholders = ", ".join(["%s"] * len(gap_ids))
                    await cur.execute(
                        "UPDATE knowledge_gaps "
                        "SET status = 'resolved', resolved_at = NOW(), "
                        "updated_at = NOW() "
                        f"WHERE status = 'open' AND gap_id IN ({placeholders})",
                        tuple(gap_ids),
                    )
                elif kb_id is not None:
                    await cur.execute(
                        "UPDATE knowledge_gaps "
                        "SET status = 'resolved', resolved_at = NOW(), "
                        "updated_at = NOW() "
                        "WHERE status = 'open' AND kb_id = %s",
                        (kb_id,),
                    )
                else:
                    return 0
                affected = cur.rowcount
                await conn.commit()
                return affected
