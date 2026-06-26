"""MySQL 会话持久化数据访问层。

使用 aiomysql 异步驱动 + DictCursor（字段名访问）。
三表设计：sessions（会话元信息）+ agent_states（每个 agent 独立状态）+ messages（消息）
"""
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiomysql
from agentscope.state import AgentState

logger = logging.getLogger(__name__)


class SessionDAO:
    """MySQL 会话持久化数据访问层"""

    def __init__(self, pool: aiomysql.Pool):
        self.pool = pool

    # ================================================================
    # 会话存在检查
    # ================================================================

    async def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT 1 FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                row = await cur.fetchone()
                return row is not None

    # ================================================================
    # AgentState 持久化
    # ================================================================

    async def load_agent_state(
        self, session_id: str, agent_id: str = "general_agent"
    ) -> Optional[dict]:
        """从 agent_states 表按 (session_id, agent_id) 加载 AgentState JSON。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT state FROM agent_states "
                    "WHERE session_id = %s AND agent_id = %s",
                    (session_id, agent_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                # MySQL JSON 列返回的是字符串，需要解析
                state_val = row["state"]
                if isinstance(state_val, str):
                    return json.loads(state_val)
                return state_val

    async def save_agent_state(
        self,
        session_id: str,
        user_id: str,
        agent_id: str,
        state_dict: dict,
    ) -> None:
        """UPSERT 到 agent_states 表，同时确保 sessions 行存在。

        若 sessions 行不存在则自动创建（含会话名称提取）。
        """
        now = datetime.now(timezone.utc)
        state_json = json.dumps(state_dict, ensure_ascii=False, default=str)

        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await conn.begin()
                try:
                    # 1) 确保 sessions 行存在
                    await cur.execute(
                        "SELECT 1 FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    existing = await cur.fetchone()
                    if not existing:
                        name = self._extract_name_from_state(state_dict)
                        await cur.execute(
                            "INSERT INTO sessions "
                            "(session_id, user_id, name, created_at, updated_at) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (session_id, user_id, name, now),
                        )
                    else:
                        await cur.execute(
                            "UPDATE sessions SET updated_at = %s "
                            "WHERE session_id = %s",
                            (now, session_id),
                        )

                    # 2) UPSERT agent_states（ON DUPLICATE KEY UPDATE）
                    await cur.execute(
                        "INSERT INTO agent_states "
                        "(session_id, agent_id, state, updated_at) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "state = VALUES(state), updated_at = VALUES(updated_at)",
                        (session_id, agent_id, state_json, now),
                    )

                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise

    # ================================================================
    # 消息持久化
    # ================================================================

    async def load_messages(self, session_id: str) -> list[dict]:
        """从 messages 表查询消息列表。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT role, content, timestamp FROM messages "
                    "WHERE session_id = %s ORDER BY id ASC",
                    (session_id,),
                )
                rows = await cur.fetchall()
                return [
                    {
                        "role": r["role"],
                        "content": r["content"],
                        "timestamp": r["timestamp"].strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )[:-3]
                        if hasattr(r["timestamp"], "strftime")
                        else str(r["timestamp"]),
                    }
                    for r in rows
                ]

    async def append_messages(
        self,
        session_id: str,
        user_id: str,
        new_messages: list[dict],
    ) -> None:
        """向 messages 表插入消息 + 更新 sessions 元信息。

        若 sessions 行不存在则自动创建。
        """
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await conn.begin()
                try:
                    # 1) 确保 sessions 行存在
                    await cur.execute(
                        "SELECT name FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    row = await cur.fetchone()

                    if not row:
                        name = ""
                        for msg in new_messages:
                            if msg.get("role") == "user":
                                raw_text = msg.get("content", "")
                                name = (
                                    raw_text[:50] if len(raw_text) > 50 else raw_text
                                )
                                break
                        await cur.execute(
                            "INSERT INTO sessions "
                            "(session_id, user_id, name, created_at, updated_at) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (session_id, user_id, name, now),
                        )

                    # 2) 插入消息
                    for msg in new_messages:
                        ts_raw = msg.get("timestamp", now_str)
                        if isinstance(ts_raw, str):
                            try:
                                ts = datetime.strptime(
                                    ts_raw, "%Y-%m-%d %H:%M:%S.%f"
                                ).replace(tzinfo=timezone.utc)
                            except ValueError:
                                ts = now
                        else:
                            ts = now
                        await cur.execute(
                            "INSERT INTO messages "
                            "(session_id, role, content, timestamp) "
                            "VALUES (%s, %s, %s, %s)",
                            (session_id, msg.get("role", "user"),
                             msg.get("content", ""), ts),
                        )

                    # 3) 更新 sessions 元信息
                    await cur.execute(
                        "SELECT COUNT(*) AS cnt FROM messages "
                        "WHERE session_id = %s",
                        (session_id,),
                    )
                    count_row = await cur.fetchone()
                    count = count_row["cnt"] if count_row else 0

                    if not row:
                        name = ""
                        for msg in new_messages:
                            if msg.get("role") == "user":
                                raw_text = msg.get("content", "")
                                name = (
                                    raw_text[:50] if len(raw_text) > 50 else raw_text
                                )
                                break
                        await cur.execute(
                            "UPDATE sessions "
                            "SET name = %s, message_count = %s, updated_at = %s "
                            "WHERE session_id = %s",
                            (name, count, now, session_id),
                        )
                    else:
                        await cur.execute(
                            "UPDATE sessions "
                            "SET message_count = %s, updated_at = %s "
                            "WHERE session_id = %s",
                            (count, now, session_id),
                        )

                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise

    # ================================================================
    # Trace ID
    # ================================================================

    async def save_latest_trace_id(
        self, session_id: str, trace_id: str
    ) -> None:
        """更新 sessions.latest_trace_id。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE sessions SET latest_trace_id = %s, "
                    "updated_at = NOW() WHERE session_id = %s",
                    (trace_id, session_id),
                )

    # ================================================================
    # Session 元信息
    # ================================================================

    async def get_session_meta(self, session_id: str) -> Optional[dict]:
        """获取会话元信息。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT session_id, user_id, name, created_at, "
                    "updated_at, message_count, latest_trace_id, is_pinned "
                    "FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return {
                    "session_id": row["session_id"],
                    "user_id": row["user_id"],
                    "name": row["name"],
                    "created_at": row["created_at"].strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    )[:-3],
                    "updated_at": row["updated_at"].strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    )[:-3],
                    "message_count": row["message_count"],
                    "latest_trace_id": row["latest_trace_id"] or "",
                    "is_pinned": bool(row["is_pinned"]),
                }

    # ================================================================
    # 用户会话列表
    # ================================================================

    async def list_user_sessions(
        self,
        user_id: str,
        limit: int = 15,
        pinned_limit: int = 5,
    ) -> tuple[list[dict], list[dict]]:
        """获取用户会话列表，返回 (top_sessions, sessions)。

        top_sessions: 置顶会话（按置顶时间降序）
        sessions: 非置顶会话（按更新时间降序）
        """
        # 置顶会话
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT session_id, user_id, name, created_at, "
                    "updated_at, message_count, latest_trace_id, is_pinned "
                    "FROM sessions "
                    "WHERE user_id = %s AND is_pinned = 1 "
                    "ORDER BY pinned_at DESC "
                    "LIMIT %s",
                    (user_id, pinned_limit),
                )
                pinned_rows = await cur.fetchall()

                top_sessions = []
                pinned_ids = set()
                for row in pinned_rows:
                    pinned_ids.add(row["session_id"])
                    top_sessions.append({
                        "session_id": row["session_id"],
                        "user_id": row["user_id"],
                        "name": row["name"],
                        "created_at": row["created_at"].strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )[:-3],
                        "updated_at": row["updated_at"].strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )[:-3],
                        "message_count": row["message_count"],
                        "latest_trace_id": row["latest_trace_id"] or "",
                        "is_pinned": bool(row["is_pinned"]),
                    })

                # 非置顶会话
                fetch_limit = limit + len(top_sessions)
                await cur.execute(
                    "SELECT session_id, user_id, name, created_at, "
                    "updated_at, message_count, latest_trace_id, is_pinned "
                    "FROM sessions "
                    "WHERE user_id = %s AND is_pinned = 0 "
                    "ORDER BY updated_at DESC "
                    "LIMIT %s",
                    (user_id, fetch_limit),
                )
                recent_rows = await cur.fetchall()

                sessions = []
                for row in recent_rows:
                    if row["session_id"] in pinned_ids:
                        continue
                    if len(sessions) >= limit:
                        break
                    sessions.append({
                        "session_id": row["session_id"],
                        "user_id": row["user_id"],
                        "name": row["name"],
                        "created_at": row["created_at"].strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )[:-3],
                        "updated_at": row["updated_at"].strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )[:-3],
                        "message_count": row["message_count"],
                        "latest_trace_id": row["latest_trace_id"] or "",
                        "is_pinned": bool(row["is_pinned"]),
                    })

                return top_sessions, sessions

    # ================================================================
    # 置顶 / 取消置顶
    # ================================================================

    async def pin_session(self, user_id: str, session_id: str) -> None:
        """将会话置顶。"""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE sessions SET is_pinned = 1, pinned_at = %s, "
                    "updated_at = %s WHERE session_id = %s AND user_id = %s",
                    (now, now, session_id, user_id),
                )

    async def unpin_session(self, user_id: str, session_id: str) -> None:
        """取消会话置顶。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE sessions SET is_pinned = 0, pinned_at = NULL, "
                    "updated_at = NOW() WHERE session_id = %s AND user_id = %s",
                    (session_id, user_id),
                )

    # ================================================================
    # 删除会话
    # ================================================================

    async def delete_session(self, session_id: str, user_id: str) -> None:
        """删除会话（CASCADE 自动清理 agent_states 和 messages）。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "DELETE FROM sessions "
                    "WHERE session_id = %s AND user_id = %s",
                    (session_id, user_id),
                )

    # ================================================================
    # AgentScope 原生接口
    # ================================================================

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> Optional[dict]:
        """加载 SessionRecord（dict 格式）。

        对应 AgentScope 原生 RedisStorage.get_session() 语义。
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT s.session_id, s.user_id, s.name, s.created_at, "
                    "s.updated_at, s.message_count, s.latest_trace_id, "
                    "s.is_pinned, a.state, a.agent_id "
                    "FROM sessions s "
                    "LEFT JOIN agent_states a "
                    "ON a.session_id = s.session_id AND a.agent_id = %s "
                    "WHERE s.session_id = %s AND s.user_id = %s",
                    (agent_id, session_id, user_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                result = dict(row)
                if result.get("state") is not None:
                    state_val = result["state"]
                    if isinstance(state_val, str):
                        result["state"] = json.loads(state_val)
                return result

    async def update_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        state: AgentState,
    ) -> None:
        """将 AgentState UPSERT 到 agent_states 表。

        对应 AgentScope 原生 RedisStorage.update_session_state() 语义。
        """
        now = datetime.now(timezone.utc)
        state_json = json.dumps(
            state.model_dump(),
            ensure_ascii=False,
            default=str,
        )
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await conn.begin()
                try:
                    # 确保 sessions 行存在
                    await cur.execute(
                        "SELECT 1 FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    existing = await cur.fetchone()
                    if not existing:
                        await cur.execute(
                            "INSERT INTO sessions "
                            "(session_id, user_id, name, created_at, "
                            "updated_at) "
                            "VALUES (%s, %s, '', %s, %s)",
                            (session_id, user_id, now, now),
                        )
                    # UPSERT agent_states
                    await cur.execute(
                        "INSERT INTO agent_states "
                        "(session_id, agent_id, state, updated_at) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "state = VALUES(state), "
                        "updated_at = VALUES(updated_at)",
                        (session_id, agent_id, state_json, now),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise

    # ================================================================
    # 工具方法（与数据库无关，保持原样）
    # ================================================================

    @staticmethod
    def _extract_name_from_state(state_dict: dict) -> str:
        """从 AgentState 中提取首条用户消息作为会话名称。"""
        try:
            context = state_dict.get("context", [])
            for msg in context:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "user":
                    raw_content = msg.get("content", "")
                    if isinstance(raw_content, list):
                        text_parts = []
                        for block in raw_content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "text"
                            ):
                                text_part = block.get("text", "")
                                if text_part:
                                    text_parts.append(text_part)
                        raw_text = "\n".join(text_parts)
                    else:
                        raw_text = str(raw_content)
                    return raw_text[:50] if len(raw_text) > 50 else raw_text
        except Exception:
            pass
        return ""

    @staticmethod
    def extract_messages_from_state(state_dict: dict) -> list[dict]:
        """从 agent state dict（AgentState.model_dump()）中提取对话消息列表。

        AgentState 结构:
            { session_id, summary, context: [Msg_dict, ...],
              reply_id, cur_iter, permission_context, tool_context,
              tasks_context }

        Msg_dict 结构:
            { name, content: [block, ...], role, id, metadata,
              created_at, finished_at, usage }

        返回 [{"role": "user"/"assistant", "content": str,
                "timestamp": str}, ...]
        """
        messages = []
        try:
            context = state_dict.get("context", [])
            for msg in context:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                raw_content = msg.get("content", "")
                ts = msg.get("created_at", "")

                if isinstance(raw_content, list):
                    text_parts = []
                    for block in raw_content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "text"
                        ):
                            text_part = block.get("text", "")
                            if text_part:
                                text_parts.append(text_part)
                    content_text = "\n".join(text_parts)
                else:
                    content_text = str(raw_content)

                if role in ("user", "assistant") and content_text:
                    messages.append({
                        "role": role,
                        "content": content_text,
                        "timestamp": ts,
                    })
        except Exception as e:
            logger.error(f"[session_dao] extract_messages error: {e}")
        return messages
