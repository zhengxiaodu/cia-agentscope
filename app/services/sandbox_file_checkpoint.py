"""Record completed sandbox writes for recovery when a chat is stopped."""
import asyncio
import logging

logger = logging.getLogger(__name__)


class SandboxFileCheckpoint:
    def __init__(self, snapshot, completed):
        self.snapshot = snapshot
        self.completed = completed
        self.lock = asyncio.Lock()
        self.active = 0
        self.before = None
        self.failed = False

    async def _snapshot(self):
        try:
            return await self.snapshot()
        except Exception:
            logger.warning("中断归档快照不可用", exc_info=True)
            return None

    async def run(self, operation, *args, **kwargs):
        # Overlapping tools share one interval. Publish its changes only when
        # every writer finishes successfully. The tool bodies still run concurrently.
        async with self.lock:
            if self.active == 0:
                self.before = await self._snapshot()
                self.failed = False
            self.active += 1
        succeeded = False
        try:
            result = await operation(*args, **kwargs)
            succeeded = not isinstance(result, dict) or result.get("exit_code") == 0
            return result
        finally:
            async with self.lock:
                self.active -= 1
                self.failed |= not succeeded
                if not self.active and not self.failed and self.before is not None:
                    after = await self._snapshot()
                    if after is not None:
                        self.completed.update({
                            path: sig for path, sig in after.items()
                            if sig != self.before.get(path)
                        })
