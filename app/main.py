import os
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
    WORKSPACE_BASE_IMAGE,
    WORKSPACE_BASEDIR,
    WORKSPACE_TTL,
)
from app.services.chat_service import load_model_config
from app.services.orchestrator_service import OrchestratorService
from app.services.workspace_manager import DockerWorkspaceManager
from app.dao.mysql_session_dao import SessionDAO
from app.dao.init_mysql import init_mysql_tables
from app.services.session_service import SessionService
from app.services.langfuse_service import LangfuseService
from app.routes import auth, chat, feedback, health, mng_proxy, sessions, upload


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化模型配置
    model_config = load_model_config(MODEL_CONFIG_PATH)
    app.state.model_config = model_config

    # ---- Docker 工作区管理器 ----
    workspace_manager = DockerWorkspaceManager(
        base_image=WORKSPACE_BASE_IMAGE,
        basedir=WORKSPACE_BASEDIR,
        ttl=WORKSPACE_TTL,
    )
    app.state.workspace_manager = workspace_manager
    await workspace_manager.start_sweeper()
    print(
        f"Workspace manager initialized "
        f"(image={WORKSPACE_BASE_IMAGE}, basedir={WORKSPACE_BASEDIR}, ttl={WORKSPACE_TTL})"
    )

    # 初始化多智能体编排服务（加载智能体定义 + skill + 意图识别器）
    app.state.orchestrator_service = await OrchestratorService.create(
        model_config, workspace_manager
    )
    print("Orchestrator service initialized (multi-agent + multi-intent)")

    # ---- Redis（保留，用于其他需求） ----
    redis_client = aioredis.from_url(
        REDIS_URL,
        decode_responses=False,
    )
    app.state.redis_client = redis_client
    print(f"Redis client initialized ({REDIS_URL})")

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
    print(
        f"Session service initialized "
        f"(MySQL: {MYSQL_USER}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE})"
    )

    # 初始化 Langfuse 追踪服务（非强依赖）
    app.state.langfuse_service = LangfuseService()
    if app.state.langfuse_service.enabled:
        print("Langfuse service initialized")
    else:
        print("Langfuse service disabled (credentials not configured)")

    yield

    # 关闭工作区管理器（停清扫 + 销毁全部容器）
    await workspace_manager.stop_sweeper()
    await workspace_manager.close_all()
    print("Workspace manager closed")

    # 关闭 MySQL 连接池
    mysql_pool.close()
    await mysql_pool.wait_closed()
    print("MySQL pool closed")

    # 关闭 Redis 连接
    await redis_client.close()
    print("Redis connection closed")


app = FastAPI(lifespan=lifespan)

app.include_router(auth.router, tags=["auth"])
app.include_router(chat.router, tags=["chat"])
app.include_router(feedback.router, tags=["feedback"])
app.include_router(health.router, tags=["health"])
app.include_router(sessions.router, tags=["sessions"])
app.include_router(upload.router, tags=["upload"])
app.include_router(mng_proxy.router, tags=["mng"])

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=7010, reload=True)
