# Tasks

- [x] Task 1: 新增 `session_files` 表建表 SQL（改 `app/dao/init_mysql.py`）
  - [x]在 `INIT_SQL` 末尾追加 `CREATE TABLE IF NOT EXISTS session_files`：列 `id BIGINT AUTO_INCREMENT PRIMARY KEY`、`session_id VARCHAR(64) NOT NULL`、`name VARCHAR(255) NOT NULL`、`path VARCHAR(512) NOT NULL`、`url VARCHAR(512) NOT NULL`、`size BIGINT NOT NULL DEFAULT 0`、`media_type VARCHAR(255) NOT NULL DEFAULT 'application/octet-stream'`、`created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP`、`updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP`；外键 `session_id` → `sessions(session_id) ON DELETE CASCADE`
  - [x]追加唯一索引 `UNIQUE KEY uniq_session_file (session_id, path)`（用 `CREATE UNIQUE INDEX ... ON session_files(session_id, path)`，并在 `init_mysql_tables` 的 except 中兼容 1061 重复索引错误码）
  - [x]追加普通索引 `idx_session_files_session_id ON session_files(session_id)`

- [x] Task 2: 新增 MySQL DAO 方法（改 `app/dao/mysql_session_dao.py`）
  - [x]`async def append_session_files(self, session_id: str, files: list[dict]) -> None`：遍历 files，对每条执行 `INSERT INTO session_files (session_id, name, path, url, size, media_type) VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name), url=VALUES(url), size=VALUES(size), media_type=VALUES(media_type), updated_at=NOW()`；空列表直接 return；单连接内执行，commit/rollback
  - [x]`async def load_session_files(self, session_id: str) -> list[dict]`：`SELECT name, path, url, size, media_type, created_at FROM session_files WHERE session_id=%s ORDER BY id ASC`；将 `created_at` 格式化为 `"%Y-%m-%d %H:%M:%S.%f"[:-3]` 字符串（与 `load_messages` 一致），返回 dict 列表

- [x] Task 3: 新增模型字段（改 `app/models/session.py`）
  - [x]新增 `class SessionFile(BaseModel)`：`name: str`、`path: str`、`url: str`、`size: int`、`media_type: str`、`created_at: Optional[str] = None`
  - [x]`SessionDetailResponse` 新增 `files: List[SessionFile] = []`（默认空列表，向后兼容）

- [x] Task 4: 新增 SessionService 方法 + 改 `get_session_detail`（改 `app/services/session_service.py`）
  - [x]新增 `async def append_session_files(self, session_id: str, files: list[dict]) -> None`：转调 `self.dao.append_session_files`
  - [x]新增 `async def load_session_files(self, session_id: str) -> list[dict]`：转调 `self.dao.load_session_files`
  - [x]在 `get_session_detail` 中：权限校验通过、加载 messages 之后，调用 `await self.dao.load_session_files(session_id)`，构造 `SessionFile` 列表，填入 `SessionDetailResponse(files=...)`

- [x] Task 5: `generate_response` 落库文件元信息（改 `app/services/chat_service.py`）
  - [x]在 `yield f"data: {json.dumps({'type': 'files_generated', 'files': files_payload}, ...)}\n\n"` 之后、`trace_ready` 段之前，新增：若 `files_payload` 且 `session_service` 且 `session_id`，则 `try: await session_service.append_session_files(session_id, files_payload) except Exception: logger.warning(...)`
  - [x]不改变既有 `files_generated` 事件 payload 与 `trace_ready` 事件顺序

- [x] Task 6: 端到端静态核对
  - [x]`python -m py_compile` 通过修改的 5 个文件（init_mysql.py、mysql_session_dao.py、session.py、session_service.py、chat_service.py）
  - [x]grep 确认 `append_session_files` 在 chat_service.py 调用、在 session_service.py / mysql_session_dao.py 定义；`load_session_files` 在 get_session_detail 中调用
  - [x]核对 checklist 全部条目

# Task Dependencies
- Task 2 依赖 Task 1（需 session_files 表）
- Task 4 依赖 Task 2 与 Task 3（需 DAO 方法与模型）
- Task 5 依赖 Task 4（需 SessionService.append_session_files）
- Task 6 依赖 Task 1-5 全部完成
