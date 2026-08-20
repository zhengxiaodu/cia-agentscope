import os
from dotenv import load_dotenv

load_dotenv()

from app.utils.secret_codec import resolve as _resolve_secret

# ---- 主密钥（用于解密 .env 中 ENC(...) 形式的敏感配置）----
# 形态：32 位 hex 字符串（16 字节）。明文模式下可为空。
_CONFIG_DECRYPT_KEY_RAW = os.getenv("CONFIG_DECRYPT_KEY", "")
try:
    _CONFIG_KEY = bytes.fromhex(_CONFIG_DECRYPT_KEY_RAW) if _CONFIG_DECRYPT_KEY_RAW else b""
    if _CONFIG_KEY and len(_CONFIG_KEY) != 16:
        raise ValueError(
            f"CONFIG_DECRYPT_KEY 必须为 32 位 hex（16 字节），当前 {len(_CONFIG_KEY)} 字节"
        )
except ValueError as e:
    raise RuntimeError(f"CONFIG_DECRYPT_KEY 配置非法: {e}") from e

SKILL_CONFIG_PATH = "../config/skill_config.yml"
MODEL_CONFIG_PATH = "../config/model_config.yml"
# 多智能体与多意图编排配置
AGENT_CONFIG_PATH = "../config/agent_config.yml"
INTENT_CONFIG_PATH = "../config/intent_config.yml"

JWT_ALGORITHM = "HS256"
# 支持 ENC(...) 密文（由 CONFIG_DECRYPT_KEY 解密）或明文（向后兼容）
JWT_SECRET = _resolve_secret(os.getenv("JWT_SECRET", "please-change-this-secret"), _CONFIG_KEY)
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "8"))
JWT_REFRESH_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_EXPIRE_DAYS", "7"))

# Redis 配置（保留，用于其他需求）
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_SESSION_TTL = int(os.getenv("REDIS_SESSION_TTL", "86400"))

# MySQL 配置（会话持久化）
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
# 支持 ENC(...) 密文（由 CONFIG_DECRYPT_KEY 解密）或明文（向后兼容）
MYSQL_PASSWORD = _resolve_secret(os.getenv("MYSQL_PASSWORD", ""), _CONFIG_KEY)
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "agentscope")

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
    "application/msword",  # doc
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

# 日志配置
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(64 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "7"))

# 管理中心 - 用户鉴权（登录/注册）地址
MNG_AUTH_URL = os.getenv("MNG_AUTH_URL", "")
# 管理中心 - 意图与卡片地址
MNG_INTENT_URL = os.getenv("MNG_INTENT_URL", "")

# ---- 工作区后端选择 ----
# "docker": 使用 agentscope DockerWorkspace（单机 Docker）
# "opensandbox": 使用 OpenSandbox SDK（K8s 沙箱集群）
WORKSPACE_BACKEND = os.getenv("WORKSPACE_BACKEND", "docker")

# Docker 工作区管理器配置
WORKSPACE_BASE_IMAGE = os.getenv("WORKSPACE_BASE_IMAGE", "python:3.13-slim")
WORKSPACE_BASEDIR = os.getenv("WORKSPACE_BASEDIR", "/data/docker-workspaces")
WORKSPACE_TTL = float(os.getenv("WORKSPACE_TTL", "3600"))
WORKSPACE_RETENTION_DAYS = int(os.getenv("WORKSPACE_RETENTION_DAYS", "7"))
WORKSPACE_CLEANUP_INTERVAL_HOURS = int(os.getenv("WORKSPACE_CLEANUP_INTERVAL_HOURS", "24"))

# Python 包安装源（为空则不设置容器 env）
PIP_INDEX_URL = os.getenv("PIP_INDEX_URL", "")
PIP_TRUSTED_HOST = os.getenv("PIP_TRUSTED_HOST", "")

# ---- OpenSandbox 沙箱配置 ----
OPENSANDBOX_DOMAIN = os.getenv("OPENSANDBOX_DOMAIN", "localhost:9080")
OPENSANDBOX_API_KEY = os.getenv("OPENSANDBOX_API_KEY", "")
OPENSANDBOX_PROTOCOL = os.getenv("OPENSANDBOX_PROTOCOL", "http")
OPENSANDBOX_USE_SERVER_PROXY = os.getenv("OPENSANDBOX_USE_SERVER_PROXY", "true").lower() == "true"
OPENSANDBOX_IMAGE = os.getenv("OPENSANDBOX_IMAGE", "python:3.13-slim")
OPENSANDBOX_RESOURCE_CPU = os.getenv("OPENSANDBOX_RESOURCE_CPU", "100m")
OPENSANDBOX_RESOURCE_MEMORY = os.getenv("OPENSANDBOX_RESOURCE_MEMORY", "128Mi")
# ---- OpenSandbox 预热池配置 ----
# 提前创建 N 个空闲沙箱，请求时直接分配（0=不启用，按需创建）
OPENSANDBOX_POOL_SIZE = int(os.getenv("OPENSANDBOX_POOL_SIZE", "0"))
# 池中沙箱被取出后是否自动补充
OPENSANDBOX_POOL_REFILL = os.getenv("OPENSANDBOX_POOL_REFILL", "true").lower() == "true"

# 外部技能目录
EXTERNAL_SKILLS_DIR = os.getenv("EXTERNAL_SKILLS_DIR", "")
RAGFLOW_API_KEY = os.getenv("RAGFLOW_API_KEY", "")
RAGFLOW_BASE_URL = os.getenv("RAGFLOW_BASE_URL", "")

# 制度问答（policy_qa）配置
POLICY_QA_BASE_URL = os.getenv("POLICY_QA_BASE_URL", "http://25.59.38.160:6181")
# 权限名 → 知识库 ID 映射（JSON 格式），留空则用代码内默认映射
# 示例: POLICY_QA_KB_MAP={"金科制度问答":"123","信科制度问答":"456"}
POLICY_QA_KB_MAP = os.getenv("POLICY_QA_KB_MAP", "")

# MinerU 文档解析配置（鉴权头为 x-api-key，非 Authorization）
MINERU_API_KEY = os.getenv("MINERU_API_KEY", "")
MINERU_BASE_URL = os.getenv("MINERU_BASE_URL", "")

# ---- 安全敏感内容检测配置 ----
# 敏感检测服务地址（本地词典 + MiniCPM5 语义模型），留空则关闭检测
SENSITIVE_SERVICE_URL = os.getenv("SENSITIVE_SERVICE_URL", "")
# 语义风险阈值（0.0-1.0），默认 0.7
SENSITIVE_THRESHOLD = float(os.getenv("SENSITIVE_THRESHOLD", "0.7"))
# 调用超时（秒），超时兜底放行
SENSITIVE_TIMEOUT = float(os.getenv("SENSITIVE_TIMEOUT", "5"))
