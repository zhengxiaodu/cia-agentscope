from pydantic import BaseModel
from typing import List, Optional, Any
from datetime import datetime


class SessionMessage(BaseModel):
    role: str
    content: str
    timestamp: str
    agent_ids: List[str] = []


class SessionFile(BaseModel):
    name: str
    path: str
    url: str
    size: int
    media_type: str
    created_at: Optional[str] = None


class SessionMeta(BaseModel):
    session_id: str
    user_id: str
    name: str = ""
    created_at: str
    updated_at: str
    message_count: int
    agent_ids: List[str] = []


class SessionListResponse(BaseModel):
    sessions: List[SessionMeta]


class SessionDetailResponse(BaseModel):
    session_id: str
    created_at: str
    updated_at: str
    trace_id: Optional[str] = None
    messages: List[SessionMessage]
    files: List[SessionFile] = []