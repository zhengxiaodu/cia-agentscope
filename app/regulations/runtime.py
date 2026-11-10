"""制度问答服务进程内运行时单例。

lifespan 启动时通过 set_policy_qa_service 注入 PolicyQAService 实例，
供 tools/policy_qa_tools.py 等非路由调用方进程内取用（不依赖 FastAPI Request）。
"""

from __future__ import annotations

from app.regulations.services.policy_qa_service import PolicyQAService

_policy_qa_service: PolicyQAService | None = None


def set_policy_qa_service(svc: PolicyQAService) -> None:
    """注册制度问答服务单例（应用 lifespan 启动时调用一次）。"""
    global _policy_qa_service
    _policy_qa_service = svc


def get_policy_qa_service() -> PolicyQAService:
    """取制度问答服务单例；未初始化时抛 RuntimeError。"""
    if _policy_qa_service is None:
        raise RuntimeError("制度问答服务未初始化")
    return _policy_qa_service
