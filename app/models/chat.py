from pydantic import BaseModel
from typing import List, Dict, Any, Optional


class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    session_id: Optional[str] = None
    agent_id: Optional[str] = None


class ChatResponse(BaseModel):
    role: str
    content: str
    session_id: str = None