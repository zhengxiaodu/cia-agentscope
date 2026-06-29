from pydantic import BaseModel
from typing import List, Optional


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    user_id: str
    user_name: str
    department: str
    role: str


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int
    user_info: UserInfo
    agent_access: List[str]
    skills_blacklist: List[str]