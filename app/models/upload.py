from pydantic import BaseModel
from typing import Any


class UploadResponse(BaseModel):
    code: int = 200
    msg: str = "success"
    session_id: str
    data: dict[str, Any]


class UploadErrorResponse(BaseModel):
    code: int
    msg: str
    data: dict = {}
