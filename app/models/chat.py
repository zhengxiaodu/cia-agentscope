from pydantic import BaseModel
from typing import List, Dict, Any, Optional


class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    search_enabled: bool = True
    skills: List[str] = []  # 请求级附加技能名，绑定到本次每个新建 agent（无论是否绑定智能体）


class ChatResponse(BaseModel):
    role: str
    content: str
    session_id: str = None