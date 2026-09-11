"""上传文件（即解析）DAO。

upload_files 表记录上传文件的文件名、解析内容、session_id 与 message_id：
- 上传时插入 pending 记录（message_id 为 NULL，对话尚未开始）
- 后台解析完成后写入 parsed_content
- 问答结束回填 message_id（标记该文件已被本轮问答消费，避免重复注入）
"""
import logging
from typing import List, Optional

import aiomysql

logger = logging.getLogger(__name__)


class UploadFileDAO:
    """上传文件数据访问层"""

    def __init__(self, pool: aiomysql.Pool):
        self.pool = pool

    async def insert(
        self,
        session_id: str,
        user_id: str,
        filename: str,
        media_type: str,
        parse_type: str,
        file_size: int,
    ) -> int:
        """插入一条 pending 记录，返回自增 id。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await conn.begin()
                try:
                    await cur.execute(
                        "INSERT INTO upload_files "
                        "(session_id, user_id, filename, media_type, parse_type, "
                        "file_size, status) "
                        "VALUES (%s, %s, %s, %s, %s, %s, 'pending')",
                        (session_id, user_id, filename, media_type, parse_type, file_size),
                    )
                    file_id = cur.lastrowid
                    await conn.commit()
                    return file_id
                except Exception:
                    await conn.rollback()
                    raise

    async def mark_parsing(self, file_id: int) -> None:
        """标记为解析中。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE upload_files SET status = 'parsing', updated_at = NOW() "
                    "WHERE id = %s",
                    (file_id,),
                )
                await conn.commit()

    async def update_parse_result(
        self,
        file_id: int,
        status: str,
        parsed_content: Optional[str],
        error_message: Optional[str] = None,
    ) -> None:
        """写入解析结果（成功 completed / 失败 failed，均带 parsed_content）。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE upload_files "
                    "SET status = %s, parsed_content = %s, error_message = %s, "
                    "updated_at = NOW() WHERE id = %s",
                    (status, parsed_content, error_message, file_id),
                )
                await conn.commit()

    async def load_unbound_parsed(self, session_id: str) -> List[dict]:
        """查询该会话下未绑定消息且解析内容非空的上传文件（按上传顺序）。

        解析失败/超时的记录 parsed_content 为提示文案，同样返回——
        让 agent 诚实告知用户该文件解析失败。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT filename, parsed_content FROM upload_files "
                    "WHERE session_id = %s AND message_id IS NULL "
                    "AND parsed_content IS NOT NULL AND parsed_content != '' "
                    "ORDER BY id",
                    (session_id,),
                )
                rows = await cur.fetchall()
                await conn.commit()
                return [dict(r) for r in rows]

    async def load_unbound_parsing(self, session_id: str) -> List[dict]:
        """查询该会话下未绑定消息且仍在解析中（pending/parsing）的上传文件。

        用于问答前轮询等待解析完成；none 类型会很快写成 completed，
        长期停留 pending/parsing 的一般是 mineru/asr 在途任务。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT filename, parse_type FROM upload_files "
                    "WHERE session_id = %s AND message_id IS NULL "
                    "AND status IN ('pending', 'parsing') "
                    "ORDER BY id",
                    (session_id,),
                )
                rows = await cur.fetchall()
                await conn.commit()
                return [dict(r) for r in rows]

    async def bind_message_id(
        self,
        session_id: str,
        message_id: int,
        message_pair_id: str = None,
    ) -> int:
        """把该会话全部未绑定的上传文件绑定到本轮 user 消息，返回影响行数。

        message_pair_id 与 message_id 一并回填（本轮问答的配对标识）。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE upload_files "
                    "SET message_id = %s, message_pair_id = %s, "
                    "updated_at = NOW() "
                    "WHERE session_id = %s AND message_id IS NULL",
                    (message_id, message_pair_id, session_id),
                )
                affected = cur.rowcount
                await conn.commit()
                return affected

    async def has_unbound_files(self, session_id: str) -> bool:
        """该会话是否存在未绑定消息的上传文件（message_id IS NULL）。

        轻量 EXISTS 查询，用于"有上传文件时跳过问题改写"的判定，
        不区分解析状态/类型（字面语义：只要上传过且未被消费即算）。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT 1 FROM upload_files "
                    "WHERE session_id = %s AND message_id IS NULL LIMIT 1",
                    (session_id,),
                )
                row = await cur.fetchone()
                await conn.commit()
                return row is not None

    async def list_files_by_user(self, user_id: str) -> List[dict]:
        """查询某用户上传过的全部文件（最新在前）。

        返回每条记录的 session_id / message_id / filename / media_type /
        file_size；message_id 未绑定（对话尚未消费）时为 None。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT session_id, message_id, filename, media_type, file_size "
                    "FROM upload_files WHERE user_id = %s ORDER BY id DESC",
                    (user_id,),
                )
                rows = await cur.fetchall()
                await conn.commit()
                return [dict(r) for r in rows]

    async def list_files_by_session(self, session_id: str) -> List[dict]:
        """查询某会话的全部上传文件（按上传顺序，含未被对话消费的记录）。

        返回键：name / size / media_type / created_at / message_id /
        message_pair_id；message_id、message_pair_id 未绑定（对话尚未消费）
        时为 None。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT filename, media_type, file_size, created_at, "
                    "message_id, message_pair_id FROM upload_files "
                    "WHERE session_id = %s ORDER BY id ASC",
                    (session_id,),
                )
                rows = await cur.fetchall()
                await conn.commit()
                return [
                    {
                        "name": r["filename"],
                        "size": r["file_size"],
                        "media_type": r["media_type"],
                        "created_at": r["created_at"].strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )[:-3]
                        if hasattr(r["created_at"], "strftime")
                        else str(r["created_at"]),
                        "message_id": r.get("message_id"),
                        "message_pair_id": r.get("message_pair_id"),
                    }
                    for r in rows
                ]
