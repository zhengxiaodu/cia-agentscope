"""收藏消息 DAO。

message_favorites 表结构与 messages 相同，仅新增 favorite_id 字段：
收藏时按 (user_id, message_pair_id IN ...) 用 INSERT...SELECT 把消息
原样复制过来（一批收藏共享一个 favorite_id），取消收藏按 favorite_id
删除整组。收藏是消息副本，不随会话删除而级联。
"""
import logging
import secrets
from typing import List, Tuple

import aiomysql

from app.dao.mysql_session_dao import _parse_json_list

logger = logging.getLogger(__name__)


def _format_timestamp(value) -> str:
    """timestamp 列格式化为 'YYYY-MM-DD HH:MM:SS.mmm'（与消息风格一致）。"""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return str(value)


class MessageFavoriteDAO:
    """收藏消息数据访问层"""

    def __init__(self, pool: aiomysql.Pool):
        self.pool = pool

    async def create_favorite(
        self, user_id: str, message_pair_ids: List[str], title: str = ""
    ) -> Tuple[str, int]:
        """把该用户指定 message_pair_id 的消息复制到收藏表。

        生成 favorite_id（secrets.token_hex(8)，16 位十六进制小写），
        单语句 INSERT...SELECT 原子完成复制，返回 (favorite_id, 复制行数)。
        ORDER BY m.id 保证组内 user→assistant 顺序。
        title：收藏标题（组内各行冗余同值，取组内首条用户消息截断）。
        """
        favorite_id = secrets.token_hex(8)
        placeholders = ", ".join(["%s"] * len(message_pair_ids))
        sql = (
            "INSERT INTO message_favorites "
            "(favorite_id, title, session_id, role, content, `timestamp`, "
            "agent_ids, user_id, success, tokens, message_pair_id, "
            "citations, bocha_sum) "
            "SELECT %s, %s, m.session_id, m.role, m.content, m.`timestamp`, "
            "m.agent_ids, m.user_id, m.success, m.tokens, m.message_pair_id, "
            "m.citations, m.bocha_sum "
            f"FROM messages m WHERE m.user_id = %s "
            f"AND m.message_pair_id IN ({placeholders}) "
            "ORDER BY m.id ASC"
        )
        args = [favorite_id, title, user_id] + list(message_pair_ids)
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, args)
                copied = cur.rowcount
                await conn.commit()
                return favorite_id, copied

    async def get_first_user_message(
        self, user_id: str, message_pair_ids: List[str]
    ) -> str:
        """查这批 message_pair_ids 中最早的一条用户消息文本（收藏标题来源）。

        不带 session_id 条件（与 create_favorite 的 WHERE 对齐，支持跨会话收藏）。
        无匹配行或查询异常返回空串（标题失败不阻断创建流程）。
        """
        if not message_pair_ids:
            return ""
        placeholders = ", ".join(["%s"] * len(message_pair_ids))
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT content FROM messages "
                        f"WHERE user_id = %s "
                        f"AND message_pair_id IN ({placeholders}) "
                        "AND role = 'user' ORDER BY id ASC LIMIT 1",
                        [user_id] + list(message_pair_ids),
                    )
                    row = await cur.fetchone()
                    await conn.commit()
                    if row is None:
                        return ""
                    return str(row.get("content") or "")
        except Exception:
            logger.warning("[MessageFavoriteDAO] 查询首条用户消息失败", exc_info=True)
            return ""

    async def delete_favorite(self, user_id: str, favorite_id: str) -> int:
        """取消收藏：删除该用户该 favorite_id 的全部记录，返回删除行数。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "DELETE FROM message_favorites "
                    "WHERE user_id = %s AND favorite_id = %s",
                    (user_id, favorite_id),
                )
                deleted = cur.rowcount
                await conn.commit()
                return deleted

    async def rename_favorite(
        self, user_id: str, favorite_id: str, title: str
    ) -> int:
        """重命名收藏：更新该 favorite_id 全部行的 title，返回更新行数。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE message_favorites SET title = %s "
                    "WHERE user_id = %s AND favorite_id = %s",
                    (title, user_id, favorite_id),
                )
                updated = cur.rowcount
                await conn.commit()
                return updated

    async def list_favorites(self, user_id: str) -> List[dict]:
        """查询该用户全部收藏消息（按插入顺序 = 收藏时间顺序）。

        返回键对齐 mysql_session_dao.load_messages 的消息风格，
        另含 favorite_id、title（组内各行同值）与 session_id
        （跨会话收藏时前端可区分来源）。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT favorite_id, title, session_id, role, content, "
                    "timestamp, agent_ids, user_id, success, tokens, "
                    "message_pair_id, citations, bocha_sum "
                    "FROM message_favorites "
                    "WHERE user_id = %s ORDER BY id ASC",
                    (user_id,),
                )
                rows = await cur.fetchall()
                await conn.commit()
                return self._format_rows(rows)

    async def get_favorite_messages(
        self, user_id: str, favorite_id: str
    ) -> List[dict]:
        """查询单个收藏组的全部消息（详情接口专用，按复制顺序）。

        无匹配返回空列表；行格式与 list_favorites 一致。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT favorite_id, title, session_id, role, content, "
                    "timestamp, agent_ids, user_id, success, tokens, "
                    "message_pair_id, citations, bocha_sum "
                    "FROM message_favorites "
                    "WHERE user_id = %s AND favorite_id = %s "
                    "ORDER BY id ASC",
                    (user_id, favorite_id),
                )
                rows = await cur.fetchall()
                await conn.commit()
                return self._format_rows(rows)

    async def list_favorite_summaries(
        self, user_id: str, page: int = 1, page_size: int = 20
    ) -> Tuple[int, List[dict]]:
        """查询该用户收藏的摘要（列表接口专用，分页，不含消息内容）。

        GROUP BY favorite_id 一条 SQL 完成：title（组内同值取 MAX）、
        message_count（组内消息数）、first_message_time（组内最早消息时间）。
        ORDER BY MIN(id)：id 序 = 收藏先后顺序。

        Returns:
            (total, summaries)：total 为该用户收藏组总数（不分页），
            summaries 为当前页摘要列表。
        """
        offset = (page - 1) * page_size
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT COUNT(DISTINCT favorite_id) AS cnt "
                    "FROM message_favorites WHERE user_id = %s",
                    (user_id,),
                )
                cnt_row = await cur.fetchone()
                total = int(cnt_row["cnt"]) if cnt_row else 0

                await cur.execute(
                    "SELECT favorite_id, MAX(title) AS title, "
                    "COUNT(*) AS message_count, "
                    "MIN(`timestamp`) AS first_message_time "
                    "FROM message_favorites "
                    "WHERE user_id = %s "
                    "GROUP BY favorite_id "
                    "ORDER BY MIN(id) ASC "
                    "LIMIT %s OFFSET %s",
                    (user_id, page_size, offset),
                )
                rows = await cur.fetchall()
                await conn.commit()
                summaries = [
                    {
                        "favorite_id": r["favorite_id"],
                        "title": r.get("title") or "",
                        "message_count": int(r.get("message_count", 0) or 0),
                        "first_message_time": _format_timestamp(
                            r.get("first_message_time")
                        ),
                    }
                    for r in rows
                ]
                return total, summaries

    async def get_favorite_first_user_message(
        self, user_id: str, favorite_id: str
    ) -> str:
        """查询收藏组内首条用户消息文本（列表接口存量空标题的兜底来源）。

        无匹配行或查询异常返回空串（对齐 get_first_user_message 风格）。
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT content FROM message_favorites "
                        "WHERE user_id = %s AND favorite_id = %s "
                        "AND role = 'user' ORDER BY id ASC LIMIT 1",
                        (user_id, favorite_id),
                    )
                    row = await cur.fetchone()
                    await conn.commit()
                    if row is None:
                        return ""
                    return str(row.get("content") or "")
        except Exception:
            logger.warning(
                "[MessageFavoriteDAO] 查询收藏组首条用户消息失败",
                exc_info=True,
            )
            return ""

    @staticmethod
    def _format_rows(rows: List[dict]) -> List[dict]:
        """收藏行格式化：timestamp strftime、JSON 列解析、bool/int 归一化。"""
        return [
            {
                "favorite_id": r["favorite_id"],
                "title": r.get("title") or "",
                "session_id": r["session_id"],
                "role": r["role"],
                "content": r["content"],
                "timestamp": _format_timestamp(r["timestamp"]),
                "agent_ids": _parse_json_list(r.get("agent_ids")),
                "user_id": r.get("user_id", "") or "",
                "success": bool(r.get("success", 1)),
                "tokens": int(r.get("tokens", 0) or 0),
                "message_pair_id": r.get("message_pair_id"),
                "citations": _parse_json_list(r.get("citations")),
                "bocha_sum": _parse_json_list(r.get("bocha_sum")),
            }
            for r in rows
        ]
