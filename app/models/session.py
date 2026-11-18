from pydantic import BaseModel
from typing import List, Optional, Any
from datetime import datetime


class SessionMessage(BaseModel):
    role: str
    content: str
    timestamp: str
    agent_ids: List[str] = []
    user_id: str = ""
    success: bool = True
    tokens: int = 0
    message_pair_id: Optional[str] = None
    citations: List[Any] = []
    bocha_sum: List[Any] = []
    # 点赞点踩：1=赞 0=踩 None=未反馈（前端据此渲染按钮选中态）
    feedback: Optional[int] = None
    # 反馈评价内容（点踩后用户填写的文字说明）
    feedback_comment: Optional[str] = None
    # 有序内容片段（text / custom_component / feedback_component），前端按序渲染
    content_parts: List[Any] = []


class SessionFile(BaseModel):
    name: str
    path: str
    url: str
    size: int
    media_type: str
    created_at: Optional[str] = None
    message_id: Optional[int] = None
    message_pair_id: Optional[str] = None


class SessionUploadFile(BaseModel):
    name: str
    size: int
    media_type: str
    created_at: Optional[str] = None
    message_id: Optional[int] = None
    message_pair_id: Optional[str] = None


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
    upload_files: List[SessionUploadFile] = []