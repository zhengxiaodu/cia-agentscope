"""OpenSandbox 工具层桥接：将 OpenSandboxToolAdapter 包装为 agentscope FunctionTool。

提供 create_opensandbox_tools(adapter) 工厂函数，返回与
[Bash(), Read(), Write(), Edit(), Glob(), Grep()] 等价的 FunctionTool 列表，
供 AgentRegistry 透明使用。
"""
import logging
from typing import Optional

from agentscope.tool import FunctionTool
from agentscope.tool._response import ToolChunk, TextBlock

from app.services.opensandbox_adapter import OpenSandboxToolAdapter

logger = logging.getLogger(__name__)


def _text_chunk(text: str) -> ToolChunk:
    """构造文本结果的 ToolChunk。"""
    return ToolChunk(content=[TextBlock(text=text)])


def create_opensandbox_tools(adapter: OpenSandboxToolAdapter) -> list:
    """创建与 agentscope 内置工具等价的 OpenSandbox FunctionTool 列表。

    Args:
        adapter: 已绑定沙箱的 OpenSandboxToolAdapter 实例

    Returns:
        [bash_tool, read_tool, write_tool, edit_tool, glob_tool, grep_tool]
    """

    async def opensandbox_bash(command: str, timeout: int = 120000) -> ToolChunk:
        """Executes a bash command in the OpenSandbox sandbox and returns its output.

        Args:
            command: The bash command to execute.
            timeout: Optional timeout in milliseconds (default: 120000).
        """
        result = await adapter.bash(command, timeout=timeout // 1000)
        output_parts = []
        if result["stdout"]:
            output_parts.append(result["stdout"])
        if result["stderr"]:
            output_parts.append(f"[stderr]\n{result['stderr']}")
        if result["exit_code"] != 0:
            output_parts.append(f"[exit_code: {result['exit_code']}]")
        return _text_chunk("\n".join(output_parts) if output_parts else "(no output)")

    async def opensandbox_read(file_path: str) -> ToolChunk:
        """Reads the content of a file in the sandbox.

        Args:
            file_path: The absolute path to the file to read.
        """
        try:
            content = await adapter.read(file_path)
            return _text_chunk(content)
        except Exception as e:
            return _text_chunk(f"Error reading file: {e}")

    async def opensandbox_write(file_path: str, content: str) -> ToolChunk:
        """Writes content to a file in the sandbox. Creates parent directories if needed.

        Args:
            file_path: The absolute path to the file to write.
            content: The content to write to the file.
        """
        try:
            # 确保父目录存在
            # parent = file_path.rsplit("/", 1)[0] if "/" in file_path else "."
            parent = adapter._workdir
            await adapter.ensure_dir(parent)
            filename = file_path.rsplit("/", 1)[1] if "/" in file_path else file_path
            file_path = parent + "/" + filename
            await adapter.write(file_path, content)
            return _text_chunk(f"Successfully wrote to {file_path}")
        except Exception as e:
            return _text_chunk(f"Error writing file: {e}")

    async def opensandbox_edit(file_path: str, old_text: str, new_text: str) -> ToolChunk:
        """Edits a file by replacing old_text with new_text.

        Args:
            file_path: The absolute path to the file to edit.
            old_text: The exact text to find and replace.
            new_text: The replacement text.
        """
        try:
            parent = adapter._workdir
            filename = file_path.rsplit("/", 1)[1] if "/" in file_path else file_path
            file_path = parent + "/" + filename
            await adapter.edit(file_path, old_text, new_text)
            return _text_chunk(f"Successfully edited {file_path}")
        except Exception as e:
            return _text_chunk(f"Error editing file: {e}")

    async def opensandbox_glob(pattern: str, path: Optional[str] = None) -> ToolChunk:
        """Finds files matching a glob pattern in the sandbox.

        Args:
            pattern: The glob pattern to match (e.g., "*.py", "**/*.json").
            path: The directory to search in (defaults to workspace directory).
        """
        try:
            results = await adapter.glob(pattern, path)
            if results:
                return _text_chunk("\n".join(results))
            return _text_chunk("(no matching files)")
        except Exception as e:
            return _text_chunk(f"Error searching files: {e}")

    async def opensandbox_grep(pattern: str, path: Optional[str] = None) -> ToolChunk:
        """Searches for a text pattern in files within the sandbox.

        Args:
            pattern: The regex pattern to search for.
            path: The directory to search in (defaults to workspace directory).
        """
        try:
            result = await adapter.grep(pattern, path)
            return _text_chunk(result if result else "(no matches)")
        except Exception as e:
            return _text_chunk(f"Error grepping: {e}")

    # 构造 FunctionTool 列表
    bash_tool = FunctionTool(
        opensandbox_bash,
        name="Bash",
        description="Executes a bash command in the sandbox and returns its output.",
    )
    read_tool = FunctionTool(
        opensandbox_read,
        name="Read",
        description="Reads the content of a file in the sandbox.",
        is_read_only=True,
    )
    write_tool = FunctionTool(
        opensandbox_write,
        name="Write",
        description="Writes content to a file in the sandbox.",
    )
    edit_tool = FunctionTool(
        opensandbox_edit,
        name="Edit",
        description="Edits a file by replacing specified text in the sandbox.",
    )
    glob_tool = FunctionTool(
        opensandbox_glob,
        name="Glob",
        description="Finds files matching a glob pattern in the sandbox.",
        is_read_only=True,
    )
    grep_tool = FunctionTool(
        opensandbox_grep,
        name="Grep",
        description="Searches for a text pattern in files within the sandbox.",
        is_read_only=True,
    )

    return [bash_tool, read_tool, write_tool, edit_tool, glob_tool, grep_tool]
