from pydantic import BaseModel
from typing import Optional


class FeedbackRequest(BaseModel):
    trace_id: str
    liked: bool
    comment: Optional[str] = None


class FeedbackResponse(BaseModel):
    code: int = 200
    msg: str = "success"
    data: dict = {}