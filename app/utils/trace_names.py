"""埋点名称集中定义。禁止在业务代码中写字符串字面量。

命名约定：
- 全小写 kebab-case，层级用 '-' 分隔
- LLM 调用统一 llm-* 前缀，工具调用统一 tool-* 前缀
- 含动态部分的用 {} 占位，通过 .format() 生成
"""
from enum import StrEnum


class TraceName(StrEnum):
    # ---- 会话主链路 ----
    CHAT_RESPONSE = "chat-response"
    HISTORY_LOAD = "history-load"
    WORKSPACE_LOAD = "workspace-load"
    WORKSPACE_INIT = "workspace-initialize"
    SKILL_LOAD = "skill-load"

    # ---- 意图链路 ----
    QUERY_REWRITE = "query-rewrite"
    INTENT_RECOGNITION = "intent-recognition"
    INTENT_ORCHESTRATION = "intent-orchestration"

    # ---- 执行层 ----
    AGENT_RUN = "agent-{agent_id}"
    REACT_STEP = "react-step-{step}"
    ORCHESTRATE_PIPELINE = "orchestrate-pipeline"

    # ---- LLM（generation 类型）----
    LLM_QUERY_REWRITE = "llm-query-rewrite"
    LLM_INTENT_RECOGNITION = "llm-intent-recognition"
    LLM_INTENT_ORCHESTRATION = "llm-intent-orchestration"
    LLM_REACT_THINK = "llm-react-think"
    LLM_RECOMMENDED_QUESTIONS = "llm-recommended-questions"
    LLM_AGENT_CALL = "llm-{model_name}"

    # ---- 工具（tool 类型）----
    TOOL_CALL = "tool-{tool_name}"

    # ---- 收尾与外部依赖 ----
    RECOMMENDED_QUESTIONS = "recommended-questions"
    PERSIST_HISTORY = "persist-history"
    FILE_DETECT = "file-detect"
    HTTP_CALL = "http-{target}"
