"""智能体定义数据结构。"""
from typing import List, Optional

from pydantic import BaseModel, Field


class AgentDefinition(BaseModel):
    """单个智能体的配置定义（对应 agent_config.yml 中的一条记录）。

    Attributes:
        id: 智能体唯一标识，如 search_agent / chart_agent / general_agent
        name: 智能体显示名称
        skills: 该智能体绑定的技能目录名列表（对应 skill_config.yml 中的 skill name）
        system_prompt: 智能体的系统提示词
    """
    id: str
    name: str
    skills: List[str] = Field(default_factory=list)
    system_prompt: str = ""
    # mng 下发的 MCP 客户端配置（agent_access 项的 mcpClientConfig 原样透传：
    # authorization/code/query_params/transport_type/transport_url），
    # None 表示该智能体无 MCP
    mcp_client_config: Optional[dict] = None
