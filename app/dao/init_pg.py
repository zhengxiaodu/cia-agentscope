"""PostgreSQL 建表初始化。"""

import logging

import asyncpg

logger = logging.getLogger(__name__)

INIT_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    message_count INTEGER NOT NULL DEFAULT 0,
    latest_trace_id TEXT,
    is_pinned    BOOLEAN NOT NULL DEFAULT FALSE,
    pinned_at    TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_user_pinned
    ON sessions(user_id, pinned_at DESC) WHERE is_pinned = TRUE;

CREATE TABLE IF NOT EXISTS agent_states (
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id     TEXT NOT NULL,
    state        JSONB NOT NULL,
    updated_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, agent_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    timestamp    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(session_id, timestamp);
"""


async def init_pg_tables(pool: asyncpg.Pool) -> None:
    """在 PostgreSQL 中创建所需的表（幂等，CREATE TABLE IF NOT EXISTS）。"""
    try:
        async with pool.acquire() as conn:
            await conn.execute(INIT_SQL)
        logger.info("PostgreSQL tables initialized successfully")
    except Exception:
        logger.exception("Failed to initialize PostgreSQL tables")
        raise