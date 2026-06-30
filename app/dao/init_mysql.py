"""MySQL 建表初始化。"""
import logging

import aiomysql

logger = logging.getLogger(__name__)

INIT_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   VARCHAR(64) PRIMARY KEY,
    user_id      VARCHAR(64) NOT NULL,
    name         VARCHAR(255) NOT NULL DEFAULT '',
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    message_count INT NOT NULL DEFAULT 0,
    latest_trace_id VARCHAR(255),
    is_pinned    TINYINT(1) NOT NULL DEFAULT 0,
    pinned_at    TIMESTAMP NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_updated_at ON sessions(updated_at DESC);
CREATE INDEX idx_sessions_user_pinned
    ON sessions(user_id, pinned_at DESC);

CREATE TABLE IF NOT EXISTS agent_states (
    session_id   VARCHAR(64) NOT NULL,
    agent_id     VARCHAR(64) NOT NULL,
    state        JSON NOT NULL,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, agent_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS messages (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id   VARCHAR(64) NOT NULL,
    role         VARCHAR(32) NOT NULL,
    content      TEXT NOT NULL,
    timestamp    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_messages_session_id ON messages(session_id);
CREATE INDEX idx_messages_timestamp ON messages(session_id, timestamp);
"""


async def init_mysql_tables(pool: aiomysql.Pool) -> None:
    """在 MySQL 中创建所需的表（幂等，CREATE TABLE IF NOT EXISTS）。

    注意：索引的 CREATE INDEX 语句不包含 IF NOT EXISTS，
    如果索引已存在会抛出警告（不影响运行），通过 try/except 兼容。
    """
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 逐条执行，已存在的表/索引会跳过或报 warning
                for statement in _split_statements(INIT_SQL):
                    stmt = statement.strip()
                    if stmt:
                        try:
                            await cur.execute(stmt)
                        except Exception as exec_err:
                            # 索引已存在时忽略（MySQL 不直接支持
                            # CREATE INDEX IF NOT EXISTS）
                            code = getattr(exec_err, "args", [None])[0]
                            if isinstance(code, int) and code == 1061:
                                logger.debug(
                                    f"Index already exists, skipping: "
                                    f"{stmt[:60]}..."
                                )
                            else:
                                raise
        logger.info("MySQL tables initialized successfully")
    except Exception:
        logger.exception("Failed to initialize MySQL tables")
        raise


def _split_statements(sql: str) -> list:
    """将多语句 SQL 字符串按分号拆分为独立语句。"""
    statements = []
    current = []
    for line in sql.split("\n"):
        stripped = line.strip()
        if stripped.startswith("--") or stripped.startswith("#"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(current))
            current = []
    if current:
        remaining = "\n".join(current).strip()
        if remaining:
            statements.append(remaining)
    return statements
