from pydantic import BaseModel
from typing import Any, Dict


class ActionAuditContent(BaseModel):
    confirm: bool
    query: str = ""


class ActionAuditRequest(BaseModel):
    action: str
    content: ActionAuditContent


class ActionAuditResponse(BaseModel):
    code: int = 200
    msg: str = "success"
    data: Dict[str, Any] = {}
