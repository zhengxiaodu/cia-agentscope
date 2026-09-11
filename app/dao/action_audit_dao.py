"""动作确认审计日志 DAO。"""
import logging

import aiomysql

logger = logging.getLogger(__name__)


class ActionAuditDAO:
    """动作确认审计日志数据访问层"""

    def __init__(self, pool: aiomysql.Pool):
        self.pool = pool

    async def insert_log(
        self, userId: str, action: str, query: str, confirm: bool
    ) -> None:
        """插入一条动作确认审计记录。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await conn.begin()
                try:
                    await cur.execute(
                        "INSERT INTO action_audit "
                        "(userId, action, query, confirm) "
                        "VALUES (%s, %s, %s, %s)",
                        (userId, action, query, 1 if confirm else 0),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
