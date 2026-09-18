"""AgentState 存取模块：加载（含脏数据迁移/截断）、保存与历史枚举修正。

从 orchestrator_service.py 外移的纯函数，不依赖服务实例状态；
调用方传入 session_service 与会话参数即可。
"""
import logging
from typing import Any, Optional

from agentscope.state import AgentState

logger = logging.getLogger(__name__)

# 历史上下文保留条数（3 轮 = 6 条 user/assistant 消息），用于截断 AgentState.context
_HISTORY_KEEP_LAST = 6


def migrate_legacy_enum_values(state_dict: dict) -> dict:
    """修正历史脏数据中枚举被 str() 序列化的问题。

    根因：早期保存使用 model_dump() + json.dumps(default=str)，枚举实例
    （如 PermissionMode.BYPASS）被 str() 转成 "PermissionMode.BYPASS"
    而非 "bypass"，导致反序列化时 PermissionMode("PermissionMode.BYPASS")
    抛 ValueError。此处检测并修正为合法的枚举值。
    """
    if not isinstance(state_dict, dict):
        return state_dict

    # permission_context.mode: "PermissionMode.BYPASS" → "bypass"
    perm = state_dict.get("permission_context")
    if isinstance(perm, dict):
        mode = perm.get("mode")
        if isinstance(mode, str) and mode.startswith("PermissionMode."):
            perm["mode"] = mode.split(".", 1)[1].lower()
    return state_dict


def trim_state_context(state_dict: dict, keep_last: int = _HISTORY_KEEP_LAST) -> dict:
    """截断 AgentState.context 为最近 keep_last 条（默认 6 = 3 轮）。

    AgentState.context 是完整对话历史 Msg_dict 列表；大模型上下文有限时
    仅保留最近 keep_last 条（默认 6 = 3 轮）。仅内存截断，不落库。
    """
    if not state_dict:
        return state_dict
    ctx = state_dict.get("context")
    if isinstance(ctx, list) and len(ctx) > keep_last:
        state_dict = {**state_dict, "context": ctx[-keep_last:]}
    return state_dict


async def load_agent_state(
    session_service: Any,
    session_id: Optional[str],
    agent_id: str,
) -> Optional[AgentState]:
    """加载单个 agent 的 AgentState：从 session_service 读取 + 历史脏数据迁移
    + trim 截断 + 异常兜底。

    session_service 为空或读取失败均返回 None（调用方创建新状态）。
    反序列化失败时记录 warning（含异常栈），便于定位 schema 不兼容问题。
    """
    if not (session_service and session_id):
        return None
    try:
        state_dict = await session_service.load_agent_state(session_id, agent_id)
    except Exception:
        logger.warning(
            f"[OrchestratorService] 读取 {agent_id} 状态失败，将新建",
            exc_info=True,
        )
        return None
    if not state_dict:
        return None

    # 历史脏数据迁移：修正 "PermissionMode.BYPASS" → "bypass" 等
    state_dict = migrate_legacy_enum_values(state_dict)
    # 截断 context 为最近 N 条，控制模型输入 token
    state_dict = trim_state_context(state_dict)
    try:
        return AgentState.model_validate(state_dict)
    except Exception:
        logger.warning(
            f"[OrchestratorService] 反序列化 {agent_id} 状态失败，将新建",
            exc_info=True,
        )
        return None


async def persist_agent_state(
    session_service: Any,
    session_id: Optional[str],
    user_id: Optional[str],
    agent_id: str,
    state_dict: dict,
) -> None:
    """保存单个 agent 的 AgentState（带非空校验与异常兜底）。"""
    if not (session_service and session_id and user_id and state_dict):
        return
    try:
        await session_service.save_agent_state(
            session_id, user_id, agent_id, state_dict,
        )
    except Exception:
        logger.exception(
            f"[OrchestratorService] 保存 agent {agent_id} 状态失败"
        )
