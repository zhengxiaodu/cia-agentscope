from pydantic import BaseModel
from typing import List, Dict, Any, Optional


class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    search_enabled: bool = True
    skills: List[str] = []  # 请求级附加技能名，绑定到本次每个新建 agent（无论是否绑定智能体）
    # 前端所选知识库名列表（制度问答）；空 = 全选（用户权限内全部知识库）
    knowledge_bases: List[str] = []


class ChatResponse(BaseModel):
    role: str
    content: str
    session_id: str = None