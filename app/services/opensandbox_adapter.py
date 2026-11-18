"""OpenSandbox 适配层：封装 Sandbox 为 agentscope 工具兼容接口。

将 OpenSandbox SDK 的操作包装为与 agentscope 工具等价的调用，
供 AgentRegistry / Agent 透明使用。
"""
import base64
import logging
import posixpath
import shlex
import uuid
from datetime import timedelta
from typing import Optional

from opensandbox import Sandbox
from opensandbox.models.filesystem import WriteEntry, SearchEntry
from opensandbox.models.execd import RunCommandOpts

logger = logging.getLogger(__name__)


def _join_log_messages(messages) -> str:
    """拼接 SDK 日志消息列表为完整文本。

    logs.stdout/stderr 是 OutputMessage 列表，沙箱按行回传且每条 m.text
    不含换行符；直接 "".join 会把多行输出拼成一行（如 find 输出
    './a.md./b.md./c.docx'），导致 list_session_files/stat 等解析失败。
    故按行拼接。
    """
    return "\n".join(m.text for m in messages)


class OpenSandboxToolAdapter:
    """agentscope 工具 -> OpenSandbox 操作的适配层。

    提供与 agentscope.tool 中 Bash/Read/Write/Edit/Glob/Grep 等价的接口。
    """

    def __init__(self, sandbox: Sandbox, workdir: str = "/workspace"):
        self._sandbox = sandbox
        self._workdir = workdir

    @property
    def sandbox(self) -> Sandbox:
        return self._sandbox

    @property
    def workdir(self) -> str:
        return self._workdir

    @workdir.setter
    def workdir(self, path: str) -> None:
        self._workdir = path

    # ---- Bash 等价 ----
    async def bash(self, command: str, timeout: int = 120) -> dict:
        """执行 shell 命令，返回 {stdout, stderr, exit_code}。"""
        result = await self._sandbox.commands.run(command, opts=RunCommandOpts(
            working_directory=self._workdir, timeout=timedelta(seconds=timeout),
        ))
        stdout = _join_log_messages(result.logs.stdout)
        stderr = _join_log_messages(result.logs.stderr)
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.exit_code,
        }

    async def artifact_signatures(self) -> dict[str, tuple[str, str]]:
        """Read metadata from the current sandbox without Redis, reconnects or file downloads."""
        from app.services.file_change_detector import ALLOWED_EXTENSIONS

        extensions = "|".join(sorted(ALLOWED_EXTENSIONS))
        scan = (
            "find . -regextype posix-extended \\( -path ./uploads -o -path ./data -o -path ./skills \\) -prune "
            f"-o -type f -iregex '.*[.]({extensions})' -printf '%T@ %s %P\\0' | base64 -w0 && printf '.'"
        )
        result = await self.bash("bash -o pipefail -c " + shlex.quote(scan), timeout=30)
        output = result["stdout"]
        if result["exit_code"] != 0 or not output.endswith("."):
            raise RuntimeError("未取得完整的工作区文件快照")
        raw = base64.b64decode(output[:-1], validate=True)
        signatures = {}
        for item in raw.split(b"\0"):
            if item:
                mtime, size, path = item.decode().split(" ", 2)
                signatures[path] = (mtime, size)
        return signatures

    async def workspace_file_target(self, path: str, *, create_parent=False) -> str:
        """Resolve a tool path inside this session before Write or Edit."""
        root = self.workdir.rstrip("/") + "/"
        if path.startswith(root):
            path = path[len(root):]
        target = self._history_target(path)
        quoted = shlex.quote(target)
        command = self._history_guard(target) + (
            f'if [ -L {quoted} ] || {{ [ -e {quoted} ] && [ ! -f {quoted} ]; }}; then exit 74; fi\n'
        )
        if create_parent:
            command += 'mkdir -p -- "$parent"'
        result = await self.bash(command, timeout=30)
        if result["exit_code"] != 0:
            raise ValueError("目标路径不可写或超出当前会话工作区")
        return target

    def _history_target(self, rel_path: str) -> str:
        raw = rel_path.replace("\\", "/")
        if (not raw or raw.startswith("/") or ":" in raw or "\x00" in raw
                or any(p in (".", "..") for p in raw.split("/"))):
            raise ValueError("历史文件目标必须位于当前会话工作区内")
        return posixpath.join(self._workdir, raw)

    def _history_guard(self, target: str) -> str:
        return (
            "set -e\n"
            f"root=$(realpath -e -- {shlex.quote(self._workdir)})\n"
            f"parent=$(realpath -m -- {shlex.quote(posixpath.dirname(target))})\n"
            'case "$parent/" in "$root/"*) ;; *) exit 73 ;; esac\n'
        )

    @staticmethod
    def _parse_signature(text: str) -> tuple[str, str]:
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise RuntimeError("无法确认历史文件恢复后的签名")
        return parts[0], parts[1]

    async def file_signature(self, rel_path: str) -> tuple[str, str] | None:
        """检查文件存在性；远程失败不能当作文件不存在，拒绝符号链接。"""
        target = self._history_target(rel_path)
        quoted = shlex.quote(target)
        command = self._history_guard(target) + (
            f"if [ -L {quoted} ] || {{ [ -e {quoted} ] && [ ! -f {quoted} ]; }}; then exit 74; fi\n"
            f"if [ -f {quoted} ]; then find {quoted} -maxdepth 0 -type f -printf '%T@ %s'; fi"
        )
        result = await self.bash(command, timeout=30)
        if result['exit_code'] != 0:
            raise RuntimeError("无法检查历史文件工作区状态")
        return self._parse_signature(result['stdout']) if result['stdout'].strip() else None

    async def restore_file_if_missing(self, rel_path: str, content: bytes) -> tuple[bool, tuple[str, str]]:
        """二进制上传到临时文件，再通过原子硬链接发布，跨协程竞争也不覆盖。"""
        target = self._history_target(rel_path)
        temp = posixpath.join(self._workdir, f".history-restore-{uuid.uuid4().hex}")
        quoted = shlex.quote(target)
        command = self._history_guard(target) + (
            'mkdir -p -- "$parent"\n'
            f"if ln -T -- {shlex.quote(temp)} {quoted} 2>/dev/null; then\n"
            "  printf 'restored\\n'\n"
            f"elif [ -f {quoted} ] && [ ! -L {quoted} ]; then\n"
            "  printf 'exists\\n'\n"
            "else exit 74; fi\n"
            f"find {quoted} -maxdepth 0 -type f -printf '%T@ %s'"
        )
        try:
            # Execd 接收 600/644/755 形式的数字，0o600 会被发送为无效的 384。
            await self._sandbox.files.write_files([WriteEntry(path=temp, data=content, mode=600)])
            result = await self.bash(command, timeout=30)
            lines = result['stdout'].splitlines()
            if result['exit_code'] != 0 or len(lines) != 2 or lines[0] not in ('restored', 'exists'):
                raise RuntimeError("历史文件写入工作区失败")
            return lines[0] == 'restored', self._parse_signature(lines[1])
        finally:
            try:
                await self.bash(f"rm -f -- {shlex.quote(temp)}", timeout=30)
            except Exception:
                logger.warning("[opensandbox_adapter] 清理历史文件恢复临时文件失败", exc_info=True)

    # ---- Read 等价 ----
    async def read(self, path: str) -> str:
        """读取文件内容。"""
        return await self._sandbox.files.read_file(path)

    # ---- Read bytes 等价 ----
    async def read_bytes(self, path: str) -> bytes:
        """读取文件原始字节（execd 文件下载接口，适合图片等二进制）。

        走 HTTP 文件通道而非 shell stdout，不受命令输出大小上限影响。
        """
        return await self._sandbox.files.read_bytes(path)

    # ---- Write 等价 ----
    async def write(self, path: str, content: str) -> None:
        """写入文件。"""
        await self._sandbox.files.write_files([
            WriteEntry(path=path, data=content, mode=644)
        ])

    # ---- Edit 等价 ----
    async def edit(self, path: str, old_text: str, new_text: str) -> None:
        """读取文件 -> 替换内容 -> 写回。"""
        content = await self._sandbox.files.read_file(path)
        modified = content.replace(old_text, new_text)
        await self._sandbox.files.write_files([
            WriteEntry(path=path, data=modified, mode=644)
        ])

    # ---- Glob 等价 ----
    async def glob(self, pattern: str, path: Optional[str] = None) -> list[str]:
        """搜索匹配文件。"""
        search_path = path or self._workdir
        results = await self._sandbox.files.search(
            SearchEntry(path=search_path, pattern=pattern)
        )
        return [f.path for f in results]

    # ---- Grep 等价 ----
    async def grep(self, pattern: str, path: Optional[str] = None) -> str:
        """在文件中搜索文本。"""
        search_path = path or self._workdir
        result = await self._sandbox.commands.run(
            f"grep -rn '{pattern}' {search_path} 2>/dev/null || true"
        )
        return _join_log_messages(result.logs.stdout)

    # ---- 工作区管理 ----
    async def ensure_dir(self, path: str) -> None:
        """确保目录存在。"""
        await self._sandbox.commands.run(f"mkdir -p {path}")

    async def list_dir(self, path: Optional[str] = None) -> str:
        """列出目录内容。"""
        target = path or self._workdir
        result = await self._sandbox.commands.run(f"ls -la {target}")
        return _join_log_messages(result.logs.stdout)
