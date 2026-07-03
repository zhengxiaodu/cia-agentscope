import os
from dotenv import load_dotenv

load_dotenv()

SKILL_CONFIG_PATH = "../config/skill_config.yml"
MODEL_CONFIG_PATH = "../config/model_config.yml"
# 多智能体与多意图编排配置
AGENT_CONFIG_PATH = "../config/agent_config.yml"
INTENT_CONFIG_PATH = "../config/intent_config.yml"

JWT_ALGORITHM = "HS256"
JWT_SECRET = os.getenv("JWT_SECRET", "please-change-this-secret")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "8"))

# Redis 配置（保留，用于其他需求）
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_SESSION_TTL = int(os.getenv("REDIS_SESSION_TTL", "86400"))

# MySQL 配置（会话持久化）
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "agentscope")
MYSQL_DSN = os.getenv("MYSQL_DSN", "")

# 文件上传配置
UPLOAD_MAX_SIZE_MB = int(os.getenv("UPLOAD_MAX_SIZE_MB", "10"))
UPLOAD_ALLOWED_MEDIA_TYPES = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
    "text/plain",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "text/csv",  # csv
    "application/vnd.ms-excel",  # xls
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "application/vnd.ms-powerpoint",  # ppt
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
]

# Langfuse 可观测性配置
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com")

# 管理中心 - 用户鉴权（登录/注册）地址
MNG_AUTH_URL = os.getenv("MNG_AUTH_URL", "")
# 管理中心 - 意图与卡片地址
MNG_INTENT_URL = os.getenv("MNG_INTENT_URL", "")

# Docker 工作区管理器配置
WORKSPACE_BASE_IMAGE = os.getenv("WORKSPACE_BASE_IMAGE", "python:3.13-slim")
WORKSPACE_BASEDIR = os.getenv("WORKSPACE_BASEDIR", "/data/docker-workspaces")
WORKSPACE_TTL = float(os.getenv("WORKSPACE_TTL", "3600"))
WORKSPACE_RETENTION_DAYS = int(os.getenv("WORKSPACE_RETENTION_DAYS", "7"))
WORKSPACE_CLEANUP_INTERVAL_HOURS = int(os.getenv("WORKSPACE_CLEANUP_INTERVAL_HOURS", "24"))

# 外部技能目录
EXTERNAL_SKILLS_DIR = os.getenv("EXTERNAL_SKILLS_DIR", "")
RAGFLOW_API_KEY = os.getenv("RAGFLOW_API_KEY", "")
RAGFLOW_BASE_URL = os.getenv("RAGFLOW_BASE_URL", "")