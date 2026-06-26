import uuid
from typing import Optional

from agentscope.state import AgentState

from app.models.session import SessionMeta, SessionMessage, SessionDetailResponse


class SessionService:
    """会话生命周期管理"""

    def __init__(self, dao):
        self.dao = dao

    async def get_or_create_session(self, session_id: Optional[str], user_id: str) -> str:
        """获取已有 session_id 或创建新会话。"""
        if session_id and await self.dao.session_exists(session_id):
            return session_id
        return uuid.uuid4().hex

    async def load_agent_state(self, session_id: str, agent_id: str = "general_agent") -> Optional[dict]:
        """从 PostgreSQL 加载 AgentState 原始数据。"""
        return await self.dao.load_agent_state(session_id, agent_id)

    async def save_agent_state(self, session_id: str, user_id: str, agent_id: str, state_dict: dict) -> None:
        """将 AgentState dict 持久化到 PostgreSQL。"""
        await self.dao.save_agent_state(session_id, user_id, agent_id, state_dict)

    async def save_latest_trace_id(self, session_id: str, trace_id: str) -> None:
        """将最新 trace_id 保存到会话元信息。"""
        await self.dao.save_latest_trace_id(session_id, trace_id)

    async def load_messages(self, session_id: str) -> list[dict]:
        """加载多智能体对话历史（纯消息列表形式）。"""
        return await self.dao.load_messages(session_id)

    async def append_messages(
        self, session_id: str, user_id: str, messages: list[dict]
    ) -> None:
        """向会话历史追加消息（用户输入 + 智能体输出）。"""
        await self.dao.append_messages(session_id, user_id, messages)

    async def pin_session(self, user_id: str, session_id: str) -> None:
        """置顶会话。"""
        await self.dao.pin_session(user_id, session_id)

    async def unpin_session(self, user_id: str, session_id: str) -> None:
        """取消置顶会话。"""
        await self.dao.unpin_session(user_id, session_id)

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        """删除会话。返回 False 表示会话不存在。"""
        if not await self.dao.session_exists(session_id):
            return False
        await self.dao.delete_session(session_id, user_id)
        return True

    async def list_user_sessions(self, user_id: str, limit: int = 15) -> tuple[list[SessionMeta], list[SessionMeta]]:
        """列出用户会话，返回 (top_sessions, sessions)。"""
        raw_top, raw_list = await self.dao.list_user_sessions(user_id, limit=limit)
        return [SessionMeta(**m) for m in raw_top], [SessionMeta(**m) for m in raw_list]

    async def get_session_detail(
        self, session_id: str, user_id: str
    ) -> Optional[SessionDetailResponse]:
        """获取会话详情（含对话历史）。

        消息从 messages 表加载，不再从 AgentState 中提取。
        """
        meta = await self.dao.get_session_meta(session_id)
        if meta is None:
            return None

        if meta.get("user_id") != user_id:
            raise PermissionError("会话不属于当前用户")

        # 从 messages 表加载消息
        raw_messages = await self.dao.load_messages(session_id)
        messages = [SessionMessage(**m) for m in raw_messages]

        return SessionDetailResponse(
            session_id=session_id,
            created_at=meta.get("created_at", ""),
            updated_at=meta.get("updated_at", ""),
            trace_id=meta.get("latest_trace_id"),
            messages=messages,
        )