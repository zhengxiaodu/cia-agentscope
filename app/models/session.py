from pydantic import BaseModel
from typing import List, Optional, Any
from datetime import datetime


class SessionMessage(BaseModel):
    role: str
    content: str
    timestamp: str


class SessionMeta(BaseModel):
    session_id: str
    user_id: str
    name: str = ""
    created_at: str
    updated_at: str
    message_count: int


class SessionListResponse(BaseModel):
    sessions: List[SessionMeta]


class SessionDetailResponse(BaseModel):
    session_id: str
    created_at: str
    updated_at: str
    trace_id: Optional[str] = None
    messages: List[SessionMessage]