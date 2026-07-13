"""动作确认审计日志服务。"""
import logging

from app.dao.action_audit_dao import ActionAuditDAO

logger = logging.getLogger(__name__)


class ActionAuditService:
    """动作确认审计日志服务"""

    def __init__(self, dao: ActionAuditDAO):
        self.dao = dao

    async def record_action(
        self, userId: str, action: str, query: str, confirm: bool
    ) -> None:
        """记录一条动作确认审计日志。"""
        await self.dao.insert_log(userId, action, query, confirm)
