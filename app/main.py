import os
import uvicorn
import redis.asyncio as aioredis
import asyncpg
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.config import MODEL_CONFIG_PATH, REDIS_URL, PG_DSN
from app.services.chat_service import load_model_config
from app.services.orchestrator_service import OrchestratorService
from app.dao.pg_session_dao import SessionDAO
from app.dao.init_pg import init_pg_tables
from app.services.session_service import SessionService
from app.services.langfuse_service import LangfuseService
from app.routes import auth, chat, feedback, health, mng_proxy, sessions, upload


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化模型配置
    model_config = load_model_config(MODEL_CONFIG_PATH)
    app.state.model_config = model_config

    # 初始化多智能体编排服务（加载智能体定义 + skill + 意图识别器）
    app.state.orchestrator_service = await OrchestratorService.create(model_config)
    print("Orchestrator service initialized (multi-agent + multi-intent)")

    # ---- Redis（保留，用于其他需求） ----
    redis_client = aioredis.from_url(
        REDIS_URL,
        decode_responses=False,
    )
    app.state.redis_client = redis_client
    print(f"Redis client initialized ({REDIS_URL})")

    # ---- PostgreSQL 连接池（会话持久化） ----
    pg_pool = await asyncpg.create_pool(
        dsn=PG_DSN,
        min_size=2,
        max_size=10,
    )
    await init_pg_tables(pg_pool)  # 自动建表
    app.state.pg_pool = pg_pool
    session_dao = SessionDAO(pg_pool)
    app.state.session_dao = session_dao
    app.state.session_service = SessionService(session_dao)
    print(f"Session service initialized (PostgreSQL: {PG_DSN})")

    # 初始化 Langfuse 追踪服务（非强依赖）
    app.state.langfuse_service = LangfuseService()
    if app.state.langfuse_service.enabled:
        print("Langfuse service initialized")
    else:
        print("Langfuse service disabled (credentials not configured)")

    yield

    # 关闭 PostgreSQL 连接池
    await pg_pool.close()
    print("PostgreSQL pool closed")

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
