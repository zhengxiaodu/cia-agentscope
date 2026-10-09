"""制度问答（regulations）模块配置 — 环境变量一次性读取。

与 app/config.py 风格对齐：模块级常量、import 时读取（非函数式）。
主项目已通过 app.config 的 load_dotenv() 加载 .env，这里直接 os.getenv。
"""

import os

from app.config import LANGFUSE_HOST

# ---- 知识库服务（regulations KB API）----
REGULATIONS_KB_API_BASE_URL = os.getenv("REGULATIONS_KB_API_BASE_URL", "http://localhost:6183")
# 内部调用令牌（由服务层注入请求头），留空则不携带
REGULATIONS_KB_INTERNAL_TOKEN = os.getenv("REGULATIONS_KB_INTERNAL_TOKEN", "")

# ---- 检索参数覆盖（请求未显式指定时的兜底值）----
REGULATIONS_TOP_K_OVERRIDE = int(os.getenv("REGULATIONS_TOP_K_OVERRIDE", "20"))
REGULATIONS_RERANK_TOP_K_OVERRIDE = int(os.getenv("REGULATIONS_RERANK_TOP_K_OVERRIDE", "5"))

# ---- query 理解与改写开关 ----
# query 理解总开关（false 时直接用原 query 检索）
REGULATIONS_QUERY_UNDERSTANDING_ENABLED = os.getenv(
    "REGULATIONS_QUERY_UNDERSTANDING_ENABLED", "true"
).strip().lower() in ("1", "true", "yes", "on")
# HyDE 假设文档改写
REGULATIONS_QUERY_REWRITE_HYDE_ENABLED = os.getenv(
    "REGULATIONS_QUERY_REWRITE_HYDE_ENABLED", "true"
).strip().lower() in ("1", "true", "yes", "on")
# Step-back 退后问题改写
REGULATIONS_QUERY_REWRITE_STEPBACK_ENABLED = os.getenv(
    "REGULATIONS_QUERY_REWRITE_STEPBACK_ENABLED", "true"
).strip().lower() in ("1", "true", "yes", "on")
# LLM 意图分类
REGULATIONS_QUERY_REWRITE_LLM_CLASSIFY_ENABLED = os.getenv(
    "REGULATIONS_QUERY_REWRITE_LLM_CLASSIFY_ENABLED", "true"
).strip().lower() in ("1", "true", "yes", "on")
# 业务分类（按部门/业务域归类）
REGULATIONS_QUERY_REWRITE_BUSINESS_CLASSIFY_ENABLED = os.getenv(
    "REGULATIONS_QUERY_REWRITE_BUSINESS_CLASSIFY_ENABLED", "true"
).strip().lower() in ("1", "true", "yes", "on")
# 业务意图清单（默认关闭）
REGULATIONS_QUERY_REWRITE_BUSINESS_INTENTS_ENABLED = os.getenv(
    "REGULATIONS_QUERY_REWRITE_BUSINESS_INTENTS_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")

# ---- Langfuse 可观测性（host 复用主项目 LANGFUSE_HOST）----
REGULATIONS_LANGFUSE_PUBLIC_KEY = os.getenv("REGULATIONS_LANGFUSE_PUBLIC_KEY", "")
REGULATIONS_LANGFUSE_SECRET_KEY = os.getenv("REGULATIONS_LANGFUSE_SECRET_KEY", "")
# 总开关；public/secret key 任一为空时实际禁用（LangfuseClient 兜底）
REGULATIONS_LANGFUSE_ENABLED = os.getenv(
    "REGULATIONS_LANGFUSE_ENABLED", "true"
).strip().lower() in ("1", "true", "yes", "on")
REGULATIONS_LANGFUSE_SAMPLE_RATE = float(os.getenv("REGULATIONS_LANGFUSE_SAMPLE_RATE", "1.0"))

# ---- 数据大盘 ----
# false 时返回 mock 数据；true 依赖埋点落库后统计
REGULATIONS_DASHBOARD_USE_REAL_DATA = os.getenv(
    "REGULATIONS_DASHBOARD_USE_REAL_DATA", "false"
).strip().lower() in ("1", "true", "yes", "on")
REGULATIONS_DASHBOARD_LOOKBACK_DAYS = int(os.getenv("REGULATIONS_DASHBOARD_LOOKBACK_DAYS", "30"))


def resolve_regulations_model(model_config: dict) -> dict:
    """从已加载的 model_config.yml 字典解析制度问答模型配置。

    优先取 models.regulations_qa；整节缺失或 model_name/base_url/api_key 任一
    为空时回退 models.default。返回统一结构：
    {"provider":..., "model_name":..., "base_url":..., "api_key":..., "parameters": {...}}
    """
    models = (model_config or {}).get("models") or {}
    section = models.get("regulations_qa")
    if (
        not section
        or not section.get("model_name")
        or not section.get("base_url")
        or not section.get("api_key")
    ):
        section = models.get("default") or {}

    return {
        "provider": section.get("provider", "openai"),
        "model_name": section.get("model_name", ""),
        "base_url": section.get("base_url", ""),
        "api_key": section.get("api_key", ""),
        "parameters": section.get("parameters") or {},
    }
