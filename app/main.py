import os
import asyncio
import uvicorn
import redis.asyncio as aioredis
import aiomysql
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.config import (
    MODEL_CONFIG_PATH,
    REDIS_URL,
    MYSQL_HOST,
    MYSQL_PORT,
    MYSQL_USER,
    MYSQL_PASSWORD,
    MYSQL_DATABASE,
    WORKSPACE_TTL,
    OPENSANDBOX_DOMAIN,
    OPENSANDBOX_API_KEY,
    OPENSANDBOX_PROTOCOL,
    OPENSANDBOX_USE_SERVER_PROXY,
    OPENSANDBOX_IMAGE,
    OPENSANDBOX_RESOURCE_CPU,
    OPENSANDBOX_RESOURCE_MEMORY,
    OPENSANDBOX_POOL_SIZE,
    OPENSANDBOX_POOL_REFILL,
)
from app.services.chat_service import load_model_config
from app.services.orchestrator_service import OrchestratorService
from app.dao.mysql_session_dao import SessionDAO
from app.dao.action_audit_dao import ActionAuditDAO
from app.dao.upload_file_dao import UploadFileDAO
from app.dao.init_mysql import init_mysql_tables
from app.services.session_service import SessionService
from app.services.action_audit_service import ActionAuditService
from app.services.langfuse_service import LangfuseService
from app.routes import (
    auth, chat, feedback, files, health, mng_proxy, sessions, upload, action_audit,
    policy_qa, dashboard,
)

import logging
from app.utils.logging_setup import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # P0-2: 结构化日志初始化（必须在所有业务初始化之前，保证后续日志可被采集）
    setup_logging()

    # 初始化模型配置
    model_config = load_model_config(MODEL_CONFIG_PATH)
    app.state.model_config = model_config

    # ---- 工作区管理器（OpenSandbox，唯一后端） ----
    from datetime import timedelta
    from opensandbox.config import ConnectionConfig
    from app.services.opensandbox_workspace_manager import OpenSandboxWorkspaceManager

    osb_config = ConnectionConfig(
        domain=OPENSANDBOX_DOMAIN,
        protocol=OPENSANDBOX_PROTOCOL,
        api_key=OPENSANDBOX_API_KEY or None,
        use_server_proxy=OPENSANDBOX_USE_SERVER_PROXY,
        request_timeout=timedelta(seconds=120),
    )
    workspace_manager = OpenSandboxWorkspaceManager(
        connection_config=osb_config,
        base_image=OPENSANDBOX_IMAGE,
        basedir="/data/workspaces",
        ttl=WORKSPACE_TTL,
        resource={"cpu": OPENSANDBOX_RESOURCE_CPU, "memory": OPENSANDBOX_RESOURCE_MEMORY},
        ready_timeout=timedelta(seconds=120),
        pool_size=OPENSANDBOX_POOL_SIZE,
        pool_refill=OPENSANDBOX_POOL_REFILL,
    )
    logger.info(
        "Workspace manager initialized [opensandbox] "
        "(domain=%s, image=%s, ttl=%s, pool_size=%s)",
        OPENSANDBOX_DOMAIN, OPENSANDBOX_IMAGE, WORKSPACE_TTL, OPENSANDBOX_POOL_SIZE,
    )

    app.state.workspace_manager = workspace_manager
    await workspace_manager.start_sweeper()

    # 初始化多智能体编排服务（加载智能体定义 + skill + 意图识别器）
    app.state.orchestrator_service = await OrchestratorService.create(
        model_config, workspace_manager
    )
    logger.info("Orchestrator service initialized (multi-agent + multi-intent)")

    # ---- Redis（保留，用于其他需求） ----
    redis_client = aioredis.from_url(
        REDIS_URL,
        decode_responses=False,
    )
    app.state.redis_client = redis_client
    logger.info("Redis client initialized (%s)", REDIS_URL)

    # ---- MySQL 连接池（会话持久化） ----
    mysql_pool = await aiomysql.create_pool(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DATABASE,
        minsize=2,
        maxsize=10,
        autocommit=False,
    )
    await init_mysql_tables(mysql_pool)  # 自动建表
    app.state.mysql_pool = mysql_pool
    session_dao = SessionDAO(mysql_pool)
    app.state.session_dao = session_dao
    app.state.session_service = SessionService(session_dao)
    action_audit_dao = ActionAuditDAO(mysql_pool)
    app.state.action_audit_dao = action_audit_dao
    app.state.action_audit_service = ActionAuditService(action_audit_dao)
    app.state.upload_file_dao = UploadFileDAO(mysql_pool)
    logger.info(
        "Session service initialized "
        "(MySQL: %s@%s:%s/%s)",
        MYSQL_USER, MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE,
    )

    # ---- 制度问答（regulations）服务初始化（共用 mysql_pool 与 model_config）----
    from app.regulations.config import resolve_regulations_model
    from app.regulations.runtime import set_policy_qa_service
    from app.regulations.services.kb_client import KBClient
    from app.regulations.services.knowledge_gap_service import KnowledgeGapService
    from app.regulations.services.policy_qa_service import PolicyQAService
    from app.regulations.dao.knowledge_gap_dao import KnowledgeGapDAO

    regulations_model_cfg = resolve_regulations_model(model_config)
    kb_client = KBClient()
    gap_dao = KnowledgeGapDAO(mysql_pool)
    gap_service = KnowledgeGapService(gap_dao, regulations_model_cfg)
    policy_qa_service = PolicyQAService(regulations_model_cfg, kb_client, gap_service)
    app.state.regulations_kb_client = kb_client
    app.state.regulations_gap_dao = gap_dao
    app.state.regulations_gap_service = gap_service
    app.state.policy_qa_service = policy_qa_service
    set_policy_qa_service(policy_qa_service)
    logger.info("Regulations policy-qa service initialized")

    # 初始化 Langfuse 追踪服务（非强依赖）
    app.state.langfuse_service = LangfuseService()
    from app.services.langfuse_service import set_current_langfuse
    set_current_langfuse(app.state.langfuse_service)
    if app.state.langfuse_service.enabled:
        logger.info("Langfuse service initialized")
    else:
        logger.info("Langfuse service disabled (credentials not configured)")

    # 会话取消标志注册表：session_id → asyncio.Event（/chat/stop 触发 set，生成器检测后中断）
    app.state.chat_tasks: dict[str, asyncio.Event] = {}

    yield

    # 关闭制度问答 KB 客户端（httpx 连接池）
    try:
        await kb_client.close()
        logger.info("Regulations KB client closed")
    except Exception:
        logger.warning("Regulations KB client close failed", exc_info=True)

    # 关闭工作区管理器（停清扫 + 销毁全部容器）
    await workspace_manager.stop_sweeper()
    await workspace_manager.close_all()
    logger.info("Workspace manager closed")

    cleanup_service.stop()
    logger.info("Workspace cleanup service stopped")

    # 关闭 MySQL 连接池
    mysql_pool.close()
    await mysql_pool.wait_closed()
    logger.info("MySQL pool closed")

    # 关闭 Redis 连接
    await redis_client.close()
    logger.info("Redis connection closed")

    # P0-4: Langfuse 批量缓冲区落盘，避免滚动发布丢失未上报 trace
    try:
        app.state.langfuse_service.flush()
        logger.info("Langfuse service flushed")
    except Exception:
        logger.warning("Langfuse flush on shutdown failed", exc_info=True)


app = FastAPI(lifespan=lifespan)

app.include_router(auth.router, tags=["auth"])
app.include_router(chat.router, tags=["chat"])
app.include_router(feedback.router, tags=["feedback"])
app.include_router(files.router, tags=["files"])
app.include_router(health.router, tags=["health"])
app.include_router(sessions.router, tags=["sessions"])
app.include_router(upload.router, tags=["upload"])
app.include_router(mng_proxy.router, tags=["mng"])
app.include_router(action_audit.router, tags=["action-audit"])
app.include_router(policy_qa.router, tags=["policy-qa"])
app.include_router(dashboard.router, tags=["dashboard"])

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=7010, reload=True)
