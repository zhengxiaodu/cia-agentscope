"""工作区装配模块：获取/创建沙箱工作区并组装注册表与工具工厂。

从 orchestrator_service.py 外移。延迟导入（chart/md/policy/bocha 工具、
adapter、bridge）保持函数内导入，避免模块级循环依赖。
"""
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional

from app.agents.base import AgentDefinition
from app.agents.factory import AgentFactory
from app.agents.registry import AgentRegistry

# 联网搜索技能名（与 skill_config.yml / agent_config.yml 中的 name 一致）
_SEARCH_SKILL_NAME = "bocha_search"


@contextmanager
def _noop_ctx() -> Iterator[None]:
    """空 context manager，langfuse 未启用时作为 start_span 的占位，yield None。"""
    yield None


async def assemble_workspace_components(
    workspace_manager,
    create_model_fn,
    fused: dict,
    user_id: str,
    redis_client,
    user_id_safe: str,
    session_id_safe: str,
    search_enabled: bool = True,
    skills: Optional[List[str]] = None,
    langfuse_service: Optional[Any] = None,
) -> tuple:
    """获取/创建工作区并组装注册表与工厂（依赖沙箱，可与意图链路并行）。

    含 workspace-load 环节埋点。本函数会被 run() 放进 asyncio.create_task，
    因此不得读写请求级状态（_last_agent_ids / _last_success 等）。

    Returns:
        (registry, agent_factory)
    """
    merged_agents = fused["merged_agents"]
    merged_skills = fused["merged_skills"]
    all_skill_dirs = [s["directory"] for s in merged_skills]

    # ---- 获取/创建工作区 ----

    # 环节埋点：工作区获取/创建子 span
    ws_ctx = (
        langfuse_service.start_span(
            "workspace-load",
            input={"user_id": user_id_safe, "session_id": session_id_safe},
        )
        if langfuse_service
        else _noop_ctx()
    )
    with ws_ctx as ws_span:
        workspace = await workspace_manager.get_workspace(user_id_safe, session_id_safe)
        if workspace is None:
            # 首次创建：create_workspace 内部会在 ws.initialize() 处单独记录 workspace-initialize 子 span
            workspace = await workspace_manager.create_workspace(
                user_id=user_id_safe,
                session_id=session_id_safe,
                skill_dirs=all_skill_dirs,
                langfuse_service=langfuse_service,
            )
        if ws_span:
            try:
                ws_span.update(output={
                    "workspace_id": getattr(workspace, "workspace_id", None),
                })
            except Exception:
                pass

    from tools.chart_tools import (
        render_bar_chart, render_line_chart, render_pie_chart,
        render_generic_card, render_metric_card, render_confirm_action,
        render_indicator_table, render_selectable_list,
    )
    from agentscope.tool import FunctionTool
    from tools.md_export_tools import create_md_export_tools
    from tools.policy_qa_tools import create_policy_qa_tool

    # 工具层：根据后端选择 agentscope 原生工具 / OpenSandbox 桥接工具
    _chart_tools = [
        FunctionTool(render_pie_chart), FunctionTool(render_bar_chart),
        FunctionTool(render_line_chart), FunctionTool(render_generic_card),
        FunctionTool(render_metric_card), FunctionTool(render_confirm_action),
        FunctionTool(render_indicator_table), FunctionTool(render_selectable_list),
    ]
    # 制度问答工具（宿主侧 FunctionTool，知识库 ID 由用户权限自动映射，不依赖工作区后端）
    policy_qa_tool = create_policy_qa_tool(user_id=user_id, redis_client=redis_client)

    import base64

    from app.services.opensandbox_adapter import OpenSandboxToolAdapter
    from app.services.opensandbox_tool_bridge import create_opensandbox_tools
    # workspace 此处是 OpenSandbox Sandbox 实例
    adapter = OpenSandboxToolAdapter(
        workspace, workdir=f"/data/workspaces/{session_id_safe}"
    )

    # Markdown 导出工具的沙箱读写闭包：文本读直接走 adapter.read；
    # 二进制写经 base64 文本通道 + bash 解码落盘
    # （与 opensandbox_workspace_manager.read_session_file 的二进制读取模式对称）
    async def _sandbox_read_file(rel_path: str) -> str:
        return await adapter.read(f"{adapter.workdir}/{rel_path}")

    async def _sandbox_write_file(rel_path: str, data: bytes) -> None:
        abs_path = f"{adapter.workdir}/{rel_path}"
        b64_path = f"{abs_path}.b64"
        await adapter.write(b64_path, base64.b64encode(data).decode("ascii"))
        result = await adapter.bash(f"base64 -d '{b64_path}' > '{abs_path}'")
        # 解码成败均清理临时 b64，避免残留进入 files_generated 快照差分
        await adapter.bash(f"rm -f '{b64_path}'")
        if result["exit_code"] != 0:
            raise RuntimeError(
                f"base64 解码写入沙箱失败 exit={result['exit_code']} "
                f"stderr={result['stderr']}"
            )

    md_tools = create_md_export_tools(_sandbox_read_file, _sandbox_write_file)
    all_tools = (
        create_opensandbox_tools(adapter) + _chart_tools
        + [policy_qa_tool] + md_tools
    )
    # 联网搜索工具受请求开关控制：关闭时不注入
    # （Toolkit 的 tools 对所有 agent 全局可见，需与技能过滤同步收口）
    if search_enabled:
        from tools.bocha_search_tools import create_bocha_search_tool
        all_tools.append(create_bocha_search_tool())
    # 技能列表由管理器扫描沙箱内 /workspace/skills/ 获取
    all_skills_meta = await workspace_manager.list_skills(
        user_id=user_id_safe, session_id=session_id_safe
    )

    # 按请求开关显隐联网搜索技能（workspace 始终装载全部技能，此处按轮次过滤）
    if not search_enabled:
        all_skills_meta = [
            m for m in all_skills_meta
            if (getattr(m, "name", None) or
                (m.get("name") if isinstance(m, dict) else None)
                ) != _SEARCH_SKILL_NAME
        ]

    # ---- 6. 构建临时注册表 ----
    agent_defs = [AgentDefinition(**a) for a in merged_agents]

    # 请求级附加技能：用户请求 skills ∪（search_enabled 时追加 bocha_search）
    # bocha_search 追加到 extra 后会 union 到每个 agent；
    # search_enabled=False 时 all_skills_meta 已移除 bocha_search，
    # extra 中的声明匹配不到 loader 自动失效，行为不变
    extra_skills = list(skills or [])
    if search_enabled:
        extra_skills.append(_SEARCH_SKILL_NAME)

    registry = AgentRegistry(
        definitions=agent_defs,
        workspace=workspace,
        all_tools=all_tools,
        all_skills_meta=all_skills_meta,
        create_model_fn=create_model_fn,
        extra_skill_names=extra_skills,
    )
    agent_factory = AgentFactory(registry)
    return registry, agent_factory
