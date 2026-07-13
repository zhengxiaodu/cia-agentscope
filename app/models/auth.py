from pydantic import BaseModel
from typing import List, Optional


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    name: str
    username: str
    department: str
    password: str


class UpdateNameRequest(BaseModel):
    name: str


class UpdateDepartmentRequest(BaseModel):
    department: str


class UpdatePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserInfo(BaseModel):
    user_id: str
    username: str
    name: str
    department: str
    role: str


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int
    user_info: UserInfo
    agent_access: List[str]
    skills_blacklist: List[str]