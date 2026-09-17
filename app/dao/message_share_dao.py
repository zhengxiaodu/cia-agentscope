"""消息分享 DAO。

message_shares 表记录分享：shared_id（16 位随机十六进制）对应一个会话中
被分享的若干 message_pair_id。查看分享时凭 shared_id 取回
(session_id, 分享者 user_id, message_pair_ids)，再走会话详情接口过滤。
"""
import json
import logging
import secrets
from typing import List, Optional

import aiomysql

logger = logging.getLogger(__name__)

# shared_id 冲突重试次数（16 位十六进制 64 位随机，碰撞概率可忽略，重试仅作兜底）
_MAX_RETRY = 3


class MessageShareDAO:
    """消息分享数据访问层"""

    def __init__(self, pool: aiomysql.Pool):
        self.pool = pool

    async def create_share(
        self,
        session_id: str,
        user_id: str,
        message_pair_ids: List[str],
    ) -> str:
        """创建分享记录，返回生成的 shared_id。

        shared_id 为 secrets.token_hex(8)（16 位十六进制小写）；
        命中唯一键冲突（MySQL 1062）时换 id 重试。
        """
        pair_ids_json = json.dumps(message_pair_ids, ensure_ascii=False)
        last_err: Optional[Exception] = None
        for _ in range(_MAX_RETRY):
            shared_id = secrets.token_hex(8)
            try:
                async with self.pool.acquire() as conn:
                    async with conn.cursor(aiomysql.DictCursor) as cur:
                        await cur.execute(
                            "INSERT INTO message_shares "
                            "(shared_id, session_id, user_id, message_pair_ids) "
                            "VALUES (%s, %s, %s, %s)",
                            (shared_id, session_id, user_id, pair_ids_json),
                        )
                        await conn.commit()
                        return shared_id
            except Exception as e:
                code = getattr(e, "args", [None])[0]
                if isinstance(code, int) and code == 1062:
                    last_err = e
                    continue
                raise
        raise last_err or RuntimeError("create_share 重试耗尽")

    async def get_share(self, shared_id: str) -> Optional[dict]:
        """按 shared_id 查询分享记录。

        返回 {shared_id, session_id, user_id, message_pair_ids: list, created_at}；
        不存在返回 None。message_pair_ids 的 MySQL JSON 列可能返回 str 或
        已解析的 list，统一兜底为 list。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT shared_id, session_id, user_id, message_pair_ids, "
                    "created_at FROM message_shares WHERE shared_id = %s",
                    (shared_id,),
                )
                row = await cur.fetchone()
                await conn.commit()
                if row is None:
                    return None
                raw = row.get("message_pair_ids")
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except Exception:
                        raw = []
                if not isinstance(raw, list):
                    raw = []
                row["message_pair_ids"] = raw
                return dict(row)
