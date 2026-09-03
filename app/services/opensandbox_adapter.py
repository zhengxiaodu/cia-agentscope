"""OpenSandbox 适配层：封装 Sandbox 为 agentscope 工具兼容接口。

将 OpenSandbox SDK 的操作包装为与 agentscope 工具等价的调用，
供 AgentRegistry / Agent 透明使用。
"""
import logging
from typing import Optional

from opensandbox import Sandbox
from opensandbox.models.filesystem import WriteEntry, SearchEntry

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
        result = await self._sandbox.commands.run(command)
        stdout = _join_log_messages(result.logs.stdout)
        stderr = _join_log_messages(result.logs.stderr)
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.exit_code,
        }

    # ---- Read 等价 ----
    async def read(self, path: str) -> str:
        """读取文件内容。"""
        return await self._sandbox.files.read_file(path)

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
