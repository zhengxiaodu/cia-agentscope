import logging
import os
import shutil
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class WorkspaceCleanupService:
    """定时清理 WORKSPACE_BASEDIR 下过期 session 目录的服务。"""

    def __init__(self, basedir: str, retention_days: int, interval_hours: int):
        self.basedir = basedir
        self.retention_days = retention_days
        self.interval_hours = interval_hours
        self._scheduler = AsyncIOScheduler()

    async def cleanup(self) -> None:
        """扫描 basedir 下所有直接子目录，删除最后修改时间超过 retention_days 天的目录。"""
        if not os.path.isdir(self.basedir):
            logger.debug(f"[cleanup] basedir 不存在，跳过: {self.basedir}")
            return

        now = time.time()
        cutoff = now - self.retention_days * 86400  # 86400 秒/天
        deleted_count = 0

        for entry in os.listdir(self.basedir):
            entry_path = os.path.join(self.basedir, entry)
            if not os.path.isdir(entry_path):
                continue
            try:
                mtime = os.path.getmtime(entry_path)
                if mtime < cutoff:
                    shutil.rmtree(entry_path)
                    deleted_count += 1
                    logger.info(f"[cleanup] 已删除过期目录: {entry_path}")
            except Exception:
                logger.warning(f"[cleanup] 删除目录失败: {entry_path}", exc_info=True)

        if deleted_count > 0:
            logger.info(f"[cleanup] 本次清理共删除 {deleted_count} 个过期目录")

    def start(self) -> None:
        """启动定时清理调度器。"""
        self._scheduler.add_job(
            self.cleanup,
            trigger=IntervalTrigger(hours=self.interval_hours),
            id="workspace_cleanup",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(
            f"[cleanup] 定时清理服务已启动 "
            f"(basedir={self.basedir}, retention={self.retention_days}天, "
            f"interval={self.interval_hours}小时)"
        )

    def stop(self) -> None:
        """停止定时清理调度器。"""
        self._scheduler.shutdown(wait=False)
        logger.info("[cleanup] 定时清理服务已停止")
