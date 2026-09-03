"""OpenSandbox 工作区管理器（唯一工作区后端）。

基于 OpenSandbox SDK 实现：
- 按 user_id 分配可复用沙箱
- 按 session_id 隔离工作目录
- TTL 淘汰 + 后台 sweeper 周期清扫

高可用增强：
- 创建沙箱失败时指数退避重试（最多 3 次）
- 沙箱崩溃自动检测与重建
- 连续失败降级标记（快速失败 + 告警日志）
- 结构化监控日志（创建延迟、成功率、活跃沙箱数）
"""
import asyncio
import logging
import os
import time
from datetime import timedelta
from typing import Optional

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.filesystem import WriteEntry

from agentscope.skill import Skill

from app.services.workspace_file_access import (
    build_list_skills_command,
    parse_skills_output,
)

logger = logging.getLogger(__name__)

# ---- 高可用配置常量 ----
_MAX_CREATE_RETRIES = 3          # 创建沙箱最大重试次数
_RETRY_BASE_DELAY = 2.0          # 重试基础延迟（秒），指数退避
_DEGRADE_THRESHOLD = 5           # 连续失败 N 次后进入降级状态
_DEGRADE_RECOVERY_TIME = 60.0    # 降级后恢复探测间隔（秒）


class _Entry:
    __slots__ = (
        "sandbox", "last_access", "user_id", "session_ids", "workdir", "skills_meta",
    )

    def __init__(self, sandbox: Sandbox, user_id: str, session_ids: set | None = None):
        self.sandbox = sandbox
        self.last_access = time.monotonic()
        self.user_id = user_id
        self.session_ids = session_ids or set()
        self.workdir = "/data/workspaces"
        # 技能元信息缓存：技能只在沙箱创建时注入，沙箱生命周期内不变，
        # None 表示尚未扫描。见 list_skills。
        self.skills_meta: list[dict] | None = None


class OpenSandboxWorkspaceManager:
    """OpenSandbox 工作区管理器，底层使用 OpenSandbox SDK。
    隔离策略：同一 user_id 复用同一沙箱，不同 user_id 各自独立沙箱；
    工作路径按 session_id 隔离（沙箱内 /data/workspaces/{session_id}）。

    高可用特性：
    - 创建失败指数退避重试
    - 沙箱崩溃自动检测与重建
    - 连续失败降级保护
    - 结构化监控日志
    """

    def __init__(
        self,
        connection_config: ConnectionConfig,
        base_image: str,
        basedir: str,
        ttl: float,
        resource: dict | None = None,
        ready_timeout: timedelta = timedelta(seconds=120),
        pool_size: int = 0,
        pool_refill: bool = True,
    ):
        self._config = connection_config
        self._base_image = base_image
        self._basedir = basedir  # 沙箱内工作目录根路径
        self._ttl = ttl
        self._resource = resource or {"cpu": "100m", "memory": "128Mi"}
        self._ready_timeout = ready_timeout
        self._cache: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._struct_lock = asyncio.Lock()
        self._sweeper_task: Optional[asyncio.Task] = None
        self._sweeper_stop = asyncio.Event()
        self._sweeper_interval = max(30.0, min(self._ttl / 2, 300.0))

        # ---- 高可用状态 ----
        self._consecutive_failures = 0
        self._degraded = False
        self._degrade_since: float = 0.0
        # ---- 监控计数 ----
        self._total_created = 0
        self._total_failed = 0
        # ---- 预热池 ----
        self._pool_size = pool_size
        self._pool_refill = pool_refill
        self._warm_pool: list[Sandbox] = []
        self._pool_lock = asyncio.Lock()

    @staticmethod
    def _workspace_id(user_id: str) -> str:
        return f"user-{user_id}"

    def _session_dir(self, session_id: str) -> str:
        return f"{self._basedir}/{session_id}"

    async def _get_lock(self, wid: str) -> asyncio.Lock:
        async with self._struct_lock:
            lock = self._locks.get(wid)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[wid] = lock
            return lock

    # ---- 高可用：降级检查 ----
    def _check_degraded(self) -> bool:
        """检查是否处于降级状态，若恢复时间已到则尝试恢复。"""
        if not self._degraded:
            return False
        elapsed = time.monotonic() - self._degrade_since
        if elapsed >= _DEGRADE_RECOVERY_TIME:
            logger.info("[opensandbox_ws] 降级恢复探测：尝试退出降级状态")
            self._degraded = False
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self) -> None:
        """记录成功，重置连续失败计数。"""
        self._consecutive_failures = 0
        self._total_created += 1

    def _record_failure(self) -> None:
        """记录失败，检查是否触发降级。"""
        self._consecutive_failures += 1
        self._total_failed += 1
        if self._consecutive_failures >= _DEGRADE_THRESHOLD and not self._degraded:
            self._degraded = True
            self._degrade_since = time.monotonic()
            logger.error(
                f"[opensandbox_ws] 连续 {self._consecutive_failures} 次创建失败，"
                f"进入降级状态（{_DEGRADE_RECOVERY_TIME}s 后自动恢复探测）"
            )

    # ---- 预热池管理 ----
    async def _warm_up(self) -> None:
        """启动时预热：提前创建 pool_size 个空闲沙箱。"""
        if self._pool_size <= 0:
            return
        logger.info(f"[opensandbox_ws] 预热池启动，目标 size={self._pool_size}")
        for i in range(self._pool_size):
            try:
                sbx = await self._create_sandbox_raw()
                async with self._pool_lock:
                    self._warm_pool.append(sbx)
                logger.info(
                    f"[opensandbox_ws] 预热池补充 {i+1}/{self._pool_size} "
                    f"sandbox_id={sbx.id}"
                )
            except Exception as e:
                logger.warning(f"[opensandbox_ws] 预热池创建失败 {i+1}/{self._pool_size}: {e}")
        logger.info(f"[opensandbox_ws] 预热池就绪 size={len(self._warm_pool)}")

    async def _acquire_from_pool(self) -> Sandbox | None:
        """从预热池获取一个可用沙箱，池空或沙箱已死则返回 None。"""
        async with self._pool_lock:
            while self._warm_pool:
                sbx = self._warm_pool.pop()
                if await self._is_sandbox_alive(sbx):
                    logger.info(
                        f"[opensandbox_ws] 从预热池分配 sandbox_id={sbx.id} "
                        f"remaining={len(self._warm_pool)}"
                    )
                    # 异步补充（不阻塞当前请求）
                    if self._pool_refill:
                        asyncio.create_task(self._refill_one())
                    return sbx
                # 池中沙箱已死，销毁
                try:
                    await sbx.destroy()
                except Exception:
                    pass
        return None

    async def _refill_one(self) -> None:
        """后台补充一个沙箱到预热池。"""
        try:
            sbx = await self._create_sandbox_raw()
            async with self._pool_lock:
                if len(self._warm_pool) < self._pool_size:
                    self._warm_pool.append(sbx)
                    logger.info(
                        f"[opensandbox_ws] 预热池补充 sandbox_id={sbx.id} "
                        f"size={len(self._warm_pool)}/{self._pool_size}"
                    )
                else:
                    await sbx.destroy()
        except Exception as e:
            logger.warning(f"[opensandbox_ws] 预热池补充失败: {e}")

    async def _destroy_pool(self) -> None:
        """销毁预热池中所有空闲沙箱。"""
        async with self._pool_lock:
            for sbx in self._warm_pool:
                try:
                    await sbx.destroy()
                except Exception:
                    pass
            count = len(self._warm_pool)
            self._warm_pool.clear()
        if count:
            logger.info(f"[opensandbox_ws] 预热池已清空 destroyed={count}")

    # ---- 高可用：带重试的沙箱创建 ----
    async def _create_sandbox_with_retry(self) -> Sandbox:
        """创建沙箱：优先从预热池取，池空则新建（失败时指数退避重试）。"""
        # 优先从预热池获取
        pooled = await self._acquire_from_pool()
        if pooled is not None:
            self._record_success()
            return pooled
        # 池空，走原有创建逻辑
        return await self._create_sandbox_raw()

    async def _create_sandbox_raw(self) -> Sandbox:
        """底层沙箱创建（带指数退避重试）。"""
        last_exc = None
        for attempt in range(1, _MAX_CREATE_RETRIES + 1):
            try:
                t0 = time.monotonic()
                sbx = await Sandbox.create(
                    self._base_image,
                    connection_config=self._config,
                    timeout=timedelta(seconds=int(self._ttl)),
                    resource=self._resource,
                    ready_timeout=self._ready_timeout,
                )
                latency_ms = (time.monotonic() - t0) * 1000
                logger.info(
                    f"[opensandbox_ws] 沙箱创建成功 sandbox_id={sbx.id} "
                    f"latency={latency_ms:.0f}ms attempt={attempt}"
                )
                self._record_success()
                return sbx
            except Exception as e:
                last_exc = e
                self._record_failure()
                if attempt < _MAX_CREATE_RETRIES:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        f"[opensandbox_ws] 沙箱创建失败 attempt={attempt}/{_MAX_CREATE_RETRIES} "
                        f"error={e} retry_in={delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        f"[opensandbox_ws] 沙箱创建最终失败 attempts={_MAX_CREATE_RETRIES} "
                        f"error={e}"
                    )
        raise last_exc  # type: ignore

    # ---- 高可用：沙箱存活探测 ----
    async def _is_sandbox_alive(self, sbx: Sandbox) -> bool:
        """探测沙箱是否仍然存活。"""
        try:
            result = await sbx.commands.run("echo 1")
            return result.exit_code == 0
        except Exception:
            return False

    async def _touch_session_dir(self, sbx: Sandbox, session_id: str) -> bool:
        """建会话目录并顺带探活：一条命令同时完成 mkdir 与存活判断。

        exit_code == 0 → 沙箱存活且目录就绪；非 0 或抛异常 → 视为沙箱已死。
        把两次往返压成一次，是每轮复用路径的主要开销来源。
        """
        session_dir = self._session_dir(session_id)
        try:
            result = await sbx.commands.run(f"mkdir -p {session_dir} && echo 1")
            return result.exit_code == 0
        except Exception:
            return False

    async def _evict_locked(self, wid: str) -> None:
        entry = self._cache.pop(wid, None)
        if entry is not None:
            try:
                await entry.sandbox.destroy()
            except Exception:
                logger.exception(f"[opensandbox_ws] 销毁沙箱失败 wid={wid}")
            logger.info(f"[opensandbox_ws] 淘汰工作区 wid={wid}")

    async def _evict(self, wid: str) -> None:
        lock = await self._get_lock(wid)
        async with lock:
            await self._evict_locked(wid)

    async def create_workspace(
        self, user_id: str, session_id: str, skill_dirs: list[str] | None = None,
        langfuse_service=None,
    ) -> Sandbox:
        """创建或复用沙箱工作区。"""
        # 降级检查
        if self._check_degraded():
            logger.warning(
                f"[opensandbox_ws] 降级状态，拒绝创建 user={user_id} "
                f"(active_sandboxes={len(self._cache)})"
            )
            raise RuntimeError(
                "OpenSandbox 工作区管理器处于降级状态，请稍后重试"
            )

        wid = self._workspace_id(user_id)
        lock = await self._get_lock(wid)
        async with lock:
            # 复用已有沙箱
            entry = self._cache.get(wid)
            if entry is not None and (time.monotonic() - entry.last_access) <= self._ttl:
                # 高可用：探测沙箱存活
                if not await self._touch_session_dir(entry.sandbox, session_id):
                    logger.warning(
                        f"[opensandbox_ws] 沙箱已崩溃，重建 wid={wid}"
                    )
                    await self._evict_locked(wid)
                else:
                    entry.last_access = time.monotonic()
                    session_dir = self._session_dir(session_id)
                    entry.session_ids.add(session_id)
                    entry.workdir = session_dir
                    # 在 Sandbox 对象上设置 workdir，兼容 AgentRegistry 的 system_prompt 构建
                    entry.sandbox.workdir = session_dir
                    logger.info(f"[opensandbox_ws] 复用沙箱 wid={wid} session={session_id}")
                    return entry.sandbox

            # 创建新沙箱（带重试）
            if langfuse_service:
                with langfuse_service.start_span(
                    "workspace-initialize",
                    input={"user_id": user_id, "session_id": session_id},
                ) as init_span:
                    sbx = await self._create_sandbox_with_retry()
                    if init_span:
                        try:
                            init_span.update(output={"sandbox_id": sbx.id})
                        except Exception:
                            pass
            else:
                sbx = await self._create_sandbox_with_retry()
            session_dir = self._session_dir(session_id)
            if not await self._touch_session_dir(sbx, session_id):
                logger.warning(
                    f"[opensandbox_ws] 新建沙箱后会话目录准备失败 sandbox_id={sbx.id}"
                )

            # 注入技能文件（将宿主技能目录内容写入沙箱）
            if skill_dirs:
                await self._inject_skills(sbx, skill_dirs)

            new_entry = _Entry(sbx, user_id, session_ids={session_id})
            new_entry.workdir = session_dir
            self._cache[wid] = new_entry
            logger.info(
                f"[opensandbox_ws] 创建沙箱 wid={wid} sandbox_id={sbx.id} "
                f"active_sandboxes={len(self._cache)}"
            )
            return sbx

    async def _inject_skills(self, sbx: Sandbox, skill_dirs: list[str]) -> None:
        """将宿主技能目录内容写入沙箱 /workspace/skills/ 目录。"""
        for d in skill_dirs:
            if not d or not os.path.isdir(d):
                logger.warning(f"[opensandbox_ws] 技能目录不存在，跳过: {d}")
                continue
            skill_name = os.path.basename(d)
            for root, _, files in os.walk(d):
                for fname in files:
                    host_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(host_path, d)
                    sandbox_path = f"/workspace/skills/{skill_name}/{rel_path}"
                    try:
                        with open(host_path, "rb") as f:
                            content = f.read()
                        await sbx.files.write_files([
                            WriteEntry(path=sandbox_path, data=content, mode=644)
                        ])
                    except Exception:
                        logger.warning(f"[opensandbox_ws] 注入技能文件失败: {host_path}")

    async def get_workspace(
        self, user_id: str, session_id: str
    ) -> Optional[Sandbox]:
        """获取已有沙箱，超 TTL 或已崩溃则淘汰并返回 None。"""
        wid = self._workspace_id(user_id)
        lock = await self._get_lock(wid)
        async with lock:
            entry = self._cache.get(wid)
            if entry is None:
                return None
            if (time.monotonic() - entry.last_access) > self._ttl:
                await self._evict_locked(wid)
                return None
            # 合并探活与建目录：一次往返同时判断存活并准备会话目录
            if not await self._touch_session_dir(entry.sandbox, session_id):
                logger.warning(f"[opensandbox_ws] 沙箱已崩溃，淘汰 wid={wid}")
                await self._evict_locked(wid)
                return None
            session_dir = self._session_dir(session_id)
            entry.last_access = time.monotonic()
            entry.session_ids.add(session_id)
            entry.workdir = session_dir
            # 在 Sandbox 对象上设置 workdir，兼容 AgentRegistry 的 system_prompt 构建
            entry.sandbox.workdir = session_dir
            return entry.sandbox

    # ---- 兼容 agentscope workspace 接口 ----
    def _entry_for(self, user_id: str) -> Optional["_Entry"]:
        """按 user_id 查缓存中的沙箱 entry，无则返回 None（不回落到其他用户）。"""
        wid = self._workspace_id(user_id) if user_id else None
        return self._cache.get(wid) if wid else None

    @staticmethod
    def _stdout(result) -> str:
        """把 SDK 命令结果的 Message 列表还原为多行文本。

        注意：SDK 的 logs.stdout 是 Message 列表，每条 m.text 是一行
        （本身不含换行符），按换行拼接后才能按行解析。
        """
        return "\n".join(m.text for m in result.logs.stdout)

    @staticmethod
    def _skill_from_meta(meta: dict) -> Skill:
        """dict 元信息 → agentscope Skill 对象（Toolkit 契约）。"""
        return Skill(
            name=meta["name"],
            description=meta["description"],
            dir=meta["directory"],
            # markdown 暂不读取完整内容，skill_instruction_template
            # 只用 name/description/dir；如需完整内容可后续按需加载
            markdown="",
            updated_at=0.0,
        )

    async def list_skills(self, user_id: str = "", session_id: str = "") -> list[Skill]:
        """列出沙箱内已注入的技能元数据（单次往返 + 按沙箱缓存）。

        一条 shell 命令扫完 /workspace/skills/ 并输出 `名称<TAB>描述首行`，
        结果缓存在 _Entry.skills_meta：技能只在沙箱创建时注入，沙箱生命周期内不变。

        无该用户的沙箱时返回 []——不得回落到其他用户的沙箱（技能清单跨用户泄漏）。

        返回 agentscope Skill 对象列表，与 agentscope workspace.list_skills() 接口一致
        （Toolkit.skills_or_loaders 仅接受 str | Skill | SkillLoaderBase，不接受 dict）。

        session_id 保留在签名中仅为调用方兼容，本方法不使用。
        """
        entry = self._entry_for(user_id)
        if entry is None:
            return []

        if entry.skills_meta is not None:
            return [self._skill_from_meta(m) for m in entry.skills_meta]

        try:
            result = await entry.sandbox.commands.run(build_list_skills_command())
            skills_meta = parse_skills_output(self._stdout(result))
        except Exception:
            logger.exception("[opensandbox_ws] list_skills 失败")
            return []

        entry.skills_meta = skills_meta
        return [self._skill_from_meta(m) for m in skills_meta]

    async def list_tools(self, user_id: str = "") -> list[str]:
        """返回沙箱内可用工具列表（兼容接口）。"""
        return ["bash", "read", "write", "edit", "glob", "grep"]

    @property
    def workdir(self) -> str:
        """兼容 agentscope workspace.workdir 属性。"""
        if self._cache:
            entry = next(iter(self._cache.values()))
            return entry.workdir
        return self._basedir

    # ---- 会话文件访问（复用 OpenSandboxToolAdapter）----
    async def _get_adapter(self, user_id: str, session_id: str):
        """获取会话对应的 OpenSandboxToolAdapter（纯读取，不触发创建）。

        通过 get_workspace 查询已有沙箱；沙箱不存在/已过期/已崩溃时
        返回 None，由调用方决定返回空集合或 None。沙箱的唯一创建入口
        在 orchestrator_service.run() 内的 get_workspace → create_workspace，
        避免文件快照等只读操作产生"提前创建沙箱"的副作用。
        """
        sbx = await self.get_workspace(user_id, session_id)
        if sbx is None:
            return None
        from app.services.opensandbox_adapter import OpenSandboxToolAdapter
        return OpenSandboxToolAdapter(sbx, workdir=self._session_dir(session_id))

    # 文本类后缀（小写，无点）
    _TEXT_EXTS = {
        "md", "markdown", "txt", "text", "json", "csv", "log",
        "py", "js", "ts", "jsx", "tsx", "java", "c", "cc", "cpp", "h", "hpp",
        "go", "rs", "rb", "php", "sh", "bash", "yml", "yaml", "xml", "html",
        "htm", "css", "sql", "ini", "conf", "toml", "env",
    }

    @staticmethod
    def _is_text_rel(rel_path: str) -> bool:
        ext = os.path.splitext(rel_path)[1].lstrip(".").lower()
        return ext in OpenSandboxWorkspaceManager._TEXT_EXTS

    @staticmethod
    def _normalize_rel(rel_path: str) -> str | None:
        """规范化相对路径，禁止 .. 和绝对路径；返回 POSIX 风格相对路径或 None（非法）。"""
        if not rel_path:
            return None
        if os.path.isabs(rel_path):
            return None
        rel = rel_path.replace("\\", "/").lstrip("/")
        if rel.startswith("..") or "/.." in rel or rel == "..":
            return None
        return rel

    async def list_session_files(self, user_id: str, session_id: str) -> set[str]:
        """列出会话工作目录下所有文件的相对路径集合（POSIX /）。

        跳过顶层 data/skills 目录和 .mcp 文件；沙箱不存在/目录为空/异常返回空集合。
        """
        try:
            adapter = await self._get_adapter(user_id, session_id)
            if adapter is None:
                return set()
            session_dir = self._session_dir(session_id)
            result = await adapter.bash(
                f"cd {session_dir} && find . -type f 2>/dev/null || true"
            )
            stdout = result.get("stdout", "") if isinstance(result, dict) else ""
            files: set[str] = set()
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                # 去掉 ./ 前缀
                rel = line[2:] if line.startswith("./") else line
                rel = rel.lstrip("/")
                if not rel:
                    continue
                # 跳过顶层 data/skills 目录
                top = rel.split("/", 1)[0]
                if top in ("data", "skills"):
                    continue
                # 跳过 .mcp 文件
                if rel == ".mcp" or rel.endswith("/.mcp"):
                    continue
                files.add(rel)
            return files
        except Exception:
            logger.warning(
                f"[opensandbox_ws] list_session_files 失败 "
                f"user={user_id} session={session_id}",
                exc_info=True,
            )
            return set()

    async def read_session_file(
        self, user_id: str, session_id: str, rel_path: str
    ) -> bytes | None:
        """读取会话文件字节。文本走 adapter.read，二进制走 base64 解码。

        文件不存在返回 None；rel_path 非法（含 .. / 绝对路径）返回 None。
        """
        rel = self._normalize_rel(rel_path)
        if rel is None:
            return None
        try:
            adapter = await self._get_adapter(user_id, session_id)
            if adapter is None:
                return None
            abs_path = f"{self._session_dir(session_id)}/{rel}"
            if self._is_text_rel(rel):
                text = await adapter.read(abs_path)
                return text.encode("utf-8") if isinstance(text, str) else bytes(text)
            # 二进制：base64 解码
            result = await adapter.bash(f"base64 -w0 {abs_path} 2>/dev/null || true")
            stdout = result.get("stdout", "") if isinstance(result, dict) else ""
            stdout = stdout.strip()
            if not stdout:
                return None
            import base64
            return base64.b64decode(stdout)
        except Exception:
            logger.warning(
                f"[opensandbox_ws] read_session_file 失败 "
                f"user={user_id} session={session_id} rel={rel_path}",
                exc_info=True,
            )
            return None

    async def stat_session_file(
        self, user_id: str, session_id: str, rel_path: str
    ) -> int | None:
        """返回文件字节大小；文件不存在返回 None。"""
        rel = self._normalize_rel(rel_path)
        if rel is None:
            return None
        try:
            adapter = await self._get_adapter(user_id, session_id)
            if adapter is None:
                return None
            abs_path = f"{self._session_dir(session_id)}/{rel}"
            result = await adapter.bash(
                f"stat -c %s {abs_path} 2>/dev/null || true"
            )
            stdout = result.get("stdout", "") if isinstance(result, dict) else ""
            stdout = stdout.strip()
            if not stdout or not stdout.isdigit():
                return None
            return int(stdout)
        except Exception:
            logger.warning(
                f"[opensandbox_ws] stat_session_file 失败 "
                f"user={user_id} session={session_id} rel={rel_path}",
                exc_info=True,
            )
            return None

    async def close(self, workspace_id: str) -> None:
        await self._evict(workspace_id)

    async def close_all(self) -> None:
        # 先销毁预热池
        await self._destroy_pool()
        for wid in list(self._cache.keys()):
            await self._evict(wid)
        logger.info(
            f"[opensandbox_ws] 全部关闭 total_created={self._total_created} "
            f"total_failed={self._total_failed}"
        )

    async def start_sweeper(self) -> None:
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_stop.clear()
        self._sweeper_task = asyncio.create_task(
            self._sweeper_loop(), name="opensandbox-sweeper"
        )
        # 预热池初始化（后台执行，不阻塞启动）
        if self._pool_size > 0:
            asyncio.create_task(self._warm_up())

    async def stop_sweeper(self) -> None:
        self._sweeper_stop.set()
        if self._sweeper_task is not None:
            try:
                await asyncio.wait_for(self._sweeper_task, timeout=10)
            except asyncio.TimeoutError:
                self._sweeper_task.cancel()
            except asyncio.CancelledError:
                pass
            self._sweeper_task = None

    async def _sweeper_loop(self) -> None:
        while not self._sweeper_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._sweeper_stop.wait(), timeout=self._sweeper_interval
                )
            except asyncio.TimeoutError:
                pass
            if self._sweeper_stop.is_set():
                break
            now = time.monotonic()
            expired = [
                wid for wid, e in self._cache.items() if (now - e.last_access) > self._ttl
            ]
            for wid in expired:
                await self._evict(wid)
            if expired:
                logger.info(
                    f"[opensandbox_ws] sweeper 淘汰 {len(expired)} 个过期沙箱 "
                    f"active={len(self._cache)}"
                )
