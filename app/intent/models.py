"""意图识别层数据模型。"""
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# 意图间关系类型
RelationType = Literal["independent", "related_fixed", "related_dynamic"]

# 编排模式（由 relation 映射得到）
OrchestrationMode = Literal["parallel", "pipeline", "react"]


class Intent(BaseModel):
    """单个识别出的意图。

    Attributes:
        id: 意图标识，如 search_info / render_chart / general_chat
        query: 该意图对应的子查询（已针对该意图具体化）
        params: 额外参数（预留）
        agent: 该意图绑定的智能体 id（由 intent_config 映射填充）
    """
    id: str
    query: str
    params: Dict = Field(default_factory=dict)
    # agent 由配置层在识别后回填，LLM 不直接输出
    agent: Optional[str] = None


class IntentResult(BaseModel):
    """意图识别引擎的完整输出。

    Attributes:
        rewritten_query: 查询改写后的完整查询（独立于子意图，用于上下文/日志）
        intents: 拆解出的意图列表（1个或多个）
        relation: 意图间关系
    """
    rewritten_query: str
    intents: List[Intent]
    relation: RelationType = "independent"

    @property
    def is_single_intent(self) -> bool:
        return len(self.intents) <= 1

    @property
    def is_multi_intent(self) -> bool:
        return len(self.intents) > 1


class IntentConfig(BaseModel):
    """显式意图配置（来自 intent_config.yml）。"""
    id: str
    name: str
    description: str = ""
    agent: str
