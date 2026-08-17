"""OpenSandbox 适配层：封装 Sandbox 为 agentscope 工具兼容接口。

将 OpenSandbox SDK 的操作包装为与 agentscope 工具等价的调用，
供 AgentRegistry / Agent 透明使用。
"""
import base64
import logging
import os
from typing import Optional

from opensandbox import Sandbox
from opensandbox.models.filesystem import WriteEntry, SearchEntry

logger = logging.getLogger(__name__)


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
        stdout = "".join(m.text for m in result.logs.stdout)
        stderr = "".join(m.text for m in result.logs.stderr)
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

    # ---- Upload（用户上传二进制文件，与 write 文本写入区分）----
    async def upload(self, path: str, content: bytes) -> str:
        """将用户上传的二进制文件写入沙箱指定路径。

        与 write 区分：write 处理文本（str），upload 处理任意二进制（bytes）。
        底层仍用 self._sandbox.files.write_files，但通过 base64 文本中转 +
        沙箱内 base64 -d 解码，规避 WriteEntry.data 的 str 类型约束。
        自动确保父目录存在。

        Returns:
            写入后的目标绝对路径 path。
        """
        # 确保父目录存在
        parent = os.path.dirname(path)
        if parent:
            await self.ensure_dir(parent)
        # base64 中转：先写文本态 .b64 临时文件，再沙箱内解码到目标路径
        b64_str = base64.b64encode(content).decode("ascii")
        tmp_b64_path = f"{path}.upload.b64"
        await self._sandbox.files.write_files([
            WriteEntry(path=tmp_b64_path, data=b64_str, mode=644)
        ])
        await self._sandbox.commands.run(
            f"base64 -d {tmp_b64_path} > {path} && rm -f {tmp_b64_path}"
        )
        return path

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
        return "".join(m.text for m in result.logs.stdout)

    # ---- 工作区管理 ----
    async def ensure_dir(self, path: str) -> None:
        """确保目录存在。"""
        await self._sandbox.commands.run(f"mkdir -p {path}")

    async def list_dir(self, path: Optional[str] = None) -> str:
        """列出目录内容。"""
        target = path or self._workdir
        result = await self._sandbox.commands.run(f"ls -la {target}")
        return "".join(m.text for m in result.logs.stdout)
