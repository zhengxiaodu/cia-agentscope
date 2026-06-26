"""PostgreSQL 会话持久化数据访问层。

替代 Redis 版 SessionDAO，使用 asyncpg 连接池。
三表设计：sessions（会话元信息）+ agent_states（每个 agent 独立状态）+ messages（消息）
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import asyncpg
from agentscope.state import AgentState

from app.config import PG_DSN

logger = logging.getLogger(__name__)


class SessionDAO:
    """PostgreSQL 会话持久化数据访问层"""

    def __init__(self, pg_pool: asyncpg.Pool):
        self.pool = pg_pool

    # ================================================================
    # 会话存在检查
    # ================================================================

    async def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在。"""
        row = await self.pool.fetchrow(
            "SELECT 1 FROM sessions WHERE session_id = $1",
            session_id,
        )
        return row is not None

    # ================================================================
    # AgentState 持久化
    # ================================================================

    async def load_agent_state(self, session_id: str, agent_id: str = "general_agent") -> Optional[dict]:
        """从 agent_states 表按 (session_id, agent_id) 加载 AgentState JSON。"""
        row = await self.pool.fetchrow(
            "SELECT state FROM agent_states WHERE session_id = $1 AND agent_id = $2",
            session_id,
            agent_id,
        )
        if row is None:
            return None
        return json.loads(row["state"])

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
        now_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        state_json = json.dumps(state_dict, ensure_ascii=False, default=str)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1) 确保 sessions 行存在（首次保存时创建）
                existing = await conn.fetchrow(
                    "SELECT 1 FROM sessions WHERE session_id = $1",
                    session_id,
                )
                if not existing:
                    # 从 state 提取会话名称
                    name = self._extract_name_from_state(state_dict)
                    await conn.execute(
                        "INSERT INTO sessions (session_id, user_id, name, created_at, updated_at) "
                        "VALUES ($1, $2, $3, $4, $4)",
                        session_id,
                        user_id,
                        name,
                        now,
                    )
                else:
                    # 更新 updated_at
                    await conn.execute(
                        "UPDATE sessions SET updated_at = $1 WHERE session_id = $2",
                        now,
                        session_id,
                    )

                # 2) UPSERT agent_states
                await conn.execute(
                    "INSERT INTO agent_states (session_id, agent_id, state, updated_at) "
                    "VALUES ($1, $2, $3::jsonb, $4) "
                    "ON CONFLICT (session_id, agent_id) "
                    "DO UPDATE SET state = EXCLUDED.state, updated_at = EXCLUDED.updated_at",
                    session_id,
                    agent_id,
                    state_json,
                    now,
                )

    # ================================================================
    # 消息持久化
    # ================================================================

    async def load_messages(self, session_id: str) -> list[dict]:
        """从 messages 表查询消息列表。"""
        rows = await self.pool.fetch(
            "SELECT role, content, timestamp FROM messages "
            "WHERE session_id = $1 ORDER BY id ASC",
            session_id,
        )
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "timestamp": r["timestamp"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
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
            async with conn.transaction():
                # 1) 确保 sessions 行存在
                row = await conn.fetchrow(
                    "SELECT name FROM sessions WHERE session_id = $1",
                    session_id,
                )
                if not row:
                    # 从首条用户消息提取会话名称
                    name = ""
                    for msg in new_messages:
                        if msg.get("role") == "user":
                            raw_text = msg.get("content", "")
                            name = raw_text[:50] if len(raw_text) > 50 else raw_text
                            break
                    await conn.execute(
                        "INSERT INTO sessions (session_id, user_id, name, created_at, updated_at) "
                        "VALUES ($1, $2, $3, $4, $4)",
                        session_id,
                        user_id,
                        name,
                        now,
                    )

                # 2) 插入消息
                for msg in new_messages:
                    ts_raw = msg.get("timestamp", now_str)
                    # 字符串时间戳转 datetime
                    if isinstance(ts_raw, str):
                        try:
                            ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
                        except ValueError:
                            ts = now
                    else:
                        ts = now
                    await conn.execute(
                        "INSERT INTO messages (session_id, role, content, timestamp) "
                        "VALUES ($1, $2, $3, $4)",
                        session_id,
                        msg.get("role", "user"),
                        msg.get("content", ""),
                        ts,
                    )

                # 3) 更新 sessions 元信息
                count_row = await conn.fetchval(
                    "SELECT COUNT(*) FROM messages WHERE session_id = $1",
                    session_id,
                )
                # 如果之前没有行，提取会话名称
                if not row:
                    name = ""
                    for msg in new_messages:
                        if msg.get("role") == "user":
                            raw_text = msg.get("content", "")
                            name = raw_text[:50] if len(raw_text) > 50 else raw_text
                            break
                    await conn.execute(
                        "UPDATE sessions SET name = $1, message_count = $2, updated_at = $3 "
                        "WHERE session_id = $4",
                        name,
                        count_row,
                        now,
                        session_id,
                    )
                else:
                    await conn.execute(
                        "UPDATE sessions SET message_count = $1, updated_at = $2 "
                        "WHERE session_id = $3",
                        count_row,
                        now,
                        session_id,
                    )

    # ================================================================
    # Trace ID
    # ================================================================

    async def save_latest_trace_id(self, session_id: str, trace_id: str) -> None:
        """更新 sessions.latest_trace_id。"""
        await self.pool.execute(
            "UPDATE sessions SET latest_trace_id = $1, updated_at = NOW() "
            "WHERE session_id = $2",
            trace_id,
            session_id,
        )

    # ================================================================
    # Session 元信息
    # ================================================================

    async def get_session_meta(self, session_id: str) -> Optional[dict]:
        """获取会话元信息。"""
        row = await self.pool.fetchrow(
            "SELECT session_id, user_id, name, created_at, updated_at, "
            "       message_count, latest_trace_id, is_pinned "
            "FROM sessions WHERE session_id = $1",
            session_id,
        )
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "updated_at": row["updated_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "message_count": row["message_count"],
            "latest_trace_id": row["latest_trace_id"] or "",
            "is_pinned": row["is_pinned"],
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
        pinned_rows = await self.pool.fetch(
            "SELECT session_id, user_id, name, created_at, updated_at, "
            "       message_count, latest_trace_id, is_pinned "
            "FROM sessions "
            "WHERE user_id = $1 AND is_pinned = TRUE "
            "ORDER BY pinned_at DESC "
            "LIMIT $2",
            user_id,
            pinned_limit,
        )
        top_sessions = []
        pinned_ids = set()
        for row in pinned_rows:
            pinned_ids.add(row["session_id"])
            top_sessions.append({
                "session_id": row["session_id"],
                "user_id": row["user_id"],
                "name": row["name"],
                "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "updated_at": row["updated_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "message_count": row["message_count"],
                "latest_trace_id": row["latest_trace_id"] or "",
                "is_pinned": row["is_pinned"],
            })

        # 非置顶会话（多取一些用于补足置顶占位）
        fetch_limit = limit + len(top_sessions)
        recent_rows = await self.pool.fetch(
            "SELECT session_id, user_id, name, created_at, updated_at, "
            "       message_count, latest_trace_id, is_pinned "
            "FROM sessions "
            "WHERE user_id = $1 AND is_pinned = FALSE "
            "ORDER BY updated_at DESC "
            "LIMIT $2",
            user_id,
            fetch_limit,
        )
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
                "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "updated_at": row["updated_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "message_count": row["message_count"],
                "latest_trace_id": row["latest_trace_id"] or "",
                "is_pinned": row["is_pinned"],
            })

        return top_sessions, sessions

    # ================================================================
    # 置顶 / 取消置顶
    # ================================================================

    async def pin_session(self, user_id: str, session_id: str) -> None:
        """将会话置顶。"""
        now = datetime.now(timezone.utc)
        await self.pool.execute(
            "UPDATE sessions SET is_pinned = TRUE, pinned_at = $1, updated_at = $1 "
            "WHERE session_id = $2 AND user_id = $3",
            now,
            session_id,
            user_id,
        )

    async def unpin_session(self, user_id: str, session_id: str) -> None:
        """取消会话置顶。"""
        await self.pool.execute(
            "UPDATE sessions SET is_pinned = FALSE, pinned_at = NULL, updated_at = NOW() "
            "WHERE session_id = $1 AND user_id = $2",
            session_id,
            user_id,
        )

    # ================================================================
    # 删除会话
    # ================================================================

    async def delete_session(self, session_id: str, user_id: str) -> None:
        """删除会话（CASCADE 自动清理 agent_states 和 messages）。"""
        await self.pool.execute(
            "DELETE FROM sessions WHERE session_id = $1 AND user_id = $2",
            session_id,
            user_id,
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
        返回包含 sessions 字段 + agent_states 中对应 agent_id 的 state。
        """
        row = await self.pool.fetchrow(
            "SELECT s.session_id, s.user_id, s.name, s.created_at, s.updated_at, "
            "       s.message_count, s.latest_trace_id, s.is_pinned, "
            "       a.state, a.agent_id "
            "FROM sessions s "
            "LEFT JOIN agent_states a ON a.session_id = s.session_id AND a.agent_id = $2 "
            "WHERE s.session_id = $3 AND s.user_id = $1",
            user_id,
            agent_id,
            session_id,
        )
        if row is None:
            return None
        result = dict(row)
        # 将 state 反序列化为 dict（方便 AgentState.model_validate）
        if result.get("state") is not None:
            result["state"] = json.loads(result["state"])
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
            async with conn.transaction():
                # 确保 sessions 行存在
                existing = await conn.fetchrow(
                    "SELECT 1 FROM sessions WHERE session_id = $1",
                    session_id,
                )
                if not existing:
                    await conn.execute(
                        "INSERT INTO sessions (session_id, user_id, name, created_at, updated_at) "
                        "VALUES ($1, $2, '', $3, $3)",
                        session_id,
                        user_id,
                        now,
                    )
                # UPSERT agent_states
                await conn.execute(
                    "INSERT INTO agent_states (session_id, agent_id, state, updated_at) "
                    "VALUES ($1, $2, $3::jsonb, $4) "
                    "ON CONFLICT (session_id, agent_id) "
                    "DO UPDATE SET state = EXCLUDED.state, updated_at = EXCLUDED.updated_at",
                    session_id,
                    agent_id,
                    state_json,
                    now,
                )

    # ================================================================
    # 工具方法
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
                            if isinstance(block, dict) and block.get("type") == "text":
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
              reply_id, cur_iter, permission_context, tool_context, tasks_context }

        Msg_dict 结构:
            { name, content: [block, ...], role, id, metadata, created_at, finished_at, usage }

        返回 [{"role": "user"/"assistant", "content": str, "timestamp": str}, ...]
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
                        if isinstance(block, dict) and block.get("type") == "text":
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