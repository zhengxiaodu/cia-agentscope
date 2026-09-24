# Tasks

- [x] Task 1: 三张表加列（改 `app/dao/init_mysql.py`）
  - [x] `CREATE TABLE messages` 加 `message_pair_id VARCHAR(64) NULL DEFAULT NULL`、`citations JSON NULL`；`CREATE TABLE session_files` 加 `message_id BIGINT NULL`、`message_pair_id VARCHAR(64) NULL`；`CREATE TABLE upload_files` 加 `message_pair_id VARCHAR(64) NULL`
  - [x] 末尾 ALTER 区追加存量兼容：`ALTER TABLE messages ADD COLUMN IF NOT EXISTS (message_pair_id ...)`、`(citations ...)`、`ALTER TABLE session_files ADD COLUMN IF NOT EXISTS (message_id ...)`、`(message_pair_id ...)`、`ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS (message_pair_id ...)`（沿用 1060/1061/1064 忽略逻辑）
  - [x] 不新增索引（查询均按 session_id 走既有索引）

- [x] Task 2: 持久化目录配置（改 `app/config.py`、`.env`、`.env.example`）
  - [x] `SESSION_FILES_PERSIST_DIR = os.getenv("SESSION_FILES_PERSIST_DIR", "data/session_files")`；.env.example 加条目与注释（生成文件持久化目录，相对项目根；留空禁用持久化，url 回退沙箱地址）

- [x] Task 3: SessionDAO 扩展（改 `app/dao/mysql_session_dao.py`）
  - [x] `append_messages`：INSERT 列加 `message_pair_id`、`citations`（取 `msg.get("message_pair_id")`、`msg.get("citations")`，citations 为 list 时 `json.dumps(ensure_ascii=False)`，空写 NULL）；记录 assistant 消息自增 id；**返回值改为 `Optional[dict]`：`{"user_message_id": int|None, "assistant_message_id": int|None}`**（无消息或事务失败返回 None）
  - [x] `load_messages`：SELECT 加 `message_pair_id, citations`；citations 用 `_parse_json_list` 解析，message_pair_id 原样（NULL→None）
  - [x] `append_session_files`：INSERT 加 `message_id, message_pair_id` 两列，ON DUPLICATE KEY UPDATE 同步更新
  - [x] `load_session_files`：SELECT 加 `message_id, message_pair_id`

- [x] Task 4: UploadFileDAO 扩展（改 `app/dao/upload_file_dao.py`）
  - [x] `bind_message_id(session_id, message_id, message_pair_id)`：UPDATE 同时写 `message_id` 与 `message_pair_id`
  - [x] 新增 `list_files_by_session(session_id) -> List[dict]`：`SELECT filename, media_type, file_size, created_at, message_id, message_pair_id FROM upload_files WHERE session_id=%s ORDER BY id ASC`；created_at 格式化为 `"%Y-%m-%d %H:%M:%S.%f"[:-3]` 字符串；键名映射为 `name/size/media_type/created_at/message_id/message_pair_id`

- [x] Task 5: 模型扩展（改 `app/models/session.py`）
  - [x] `SessionMessage` 加 `message_pair_id: Optional[str] = None`、`citations: List[Any] = []`
  - [x] `SessionFile` 加 `message_id: Optional[int] = None`、`message_pair_id: Optional[str] = None`
  - [x] 新增 `SessionUploadFile(BaseModel)`：`name: str`、`size: int`、`media_type: str`、`created_at: Optional[str] = None`、`message_id: Optional[int] = None`、`message_pair_id: Optional[str] = None`
  - [x] `SessionDetailResponse` 加 `upload_files: List[SessionUploadFile] = []`

- [x] Task 6: SessionService 接线（改 `app/services/session_service.py`、`app/main.py`）
  - [x] `SessionService.__init__(self, dao, upload_file_dao=None)`
  - [x] `get_session_detail`：加载 files 之后，若 `self.upload_file_dao` 非空则 `list_files_by_session(session_id)` 构造 `SessionUploadFile` 列表填入 `upload_files`（None 时保持 `[]`）；`append_messages` 转调签名/文档同步新返回值
  - [x] `app/main.py`：lifespan 中 `UploadFileDAO(mysql_pool)` 创建提前到 `SessionService(...)` 之前，以 `upload_file_dao=` 注入

- [x] Task 7: chat_service 主流程（改 `app/services/chat_service.py`）
  - [x] `generate_response` 每轮开始（步骤②快照附近）生成 `message_pair_id = uuid.uuid4().hex`；事件解析循环新增捕获 `policy_qa_citations` 事件：`collected_citations.extend(payload.get("citations") or [])`
  - [x] `_persist_conversation_history` 增参 `message_pair_id`、`citations`；user/assistant 消息 dict 打上 `message_pair_id`，assistant 另打 `citations`（空列表传 None 不写）；正常与中断（finally）两处调用都传；内部改用返回 dict 的 `user_message_id` 回填，`bind_message_id(session_id, user_message_id, message_pair_id)`；返回 `{"user_message_id", "assistant_message_id"}` 供后续文件记录使用
  - [x] `_detect_and_emit_files` 增参 `message_pair_id`、`assistant_message_id`：diff/stat 后对每个新文件 `workspace_manager.read_session_file` 读字节流 → 写入 `{SESSION_FILES_PERSIST_DIR}/{session_id}/{rel_path}`（os.makedirs 保留子目录，新增私有 helper `_persist_file_bytes`）→ 成功则 `url=/persist-files/{session_id}/{rel_path}`，读/写失败或目录未配置则 warning 并回退 `url=/files/{session_id}/{rel_path}`；files_payload 每项带 `message_id=assistant_message_id`、`message_pair_id`；随后 yield files_generated（顺序不变）→ `append_session_files`
  - [x] 顺序保持：⑥ 持久化对话历史 → ⑦ 检测新文件+字节流搬运（session_files 依赖 messages 落库产生的 id）
  - [x] `import uuid` 及 `from app.config import SESSION_FILES_PERSIST_DIR`（或函数内读取，保持与文件现有 import 风格一致）

- [x] Task 8: 持久化文件下载接口（改 `app/routes/files.py`）
  - [x] 新增 `GET /persist-files/{session_id}/{path:path}`，参数与 `/files` 一致（mode=download|inline）
  - [x] current_user 鉴权；穿越校验复用 `/files` 的规范化逻辑（`..`/绝对路径 403）
  - [x] 读 `{SESSION_FILES_PERSIST_DIR}/{session_id}/{rel}`，不存在 404；复用 `_build_file_response`（含中文文件名 Content-Disposition 双写）

- [x] Task 9: 测试
  - [x] 新增 `tests/test_message_pair_and_citations.py`：append_messages 写 pair_id/citations 并返回双 id；load_messages 返回新字段（旧记录 null/[]）；chat_service 捕获 policy_qa_citations 事件并随 assistant 消息落库（mock orchestrator yield citations + summary 事件）；中断路径同样携带
  - [x] 新增 `tests/test_session_files_persist.py`：append_session_files 带 message_id/pair_id 的 UPSERT 与 load；`_detect_and_emit_files` 持久化成功（tmp 目录 + mock workspace_manager，url=/persist-files/...，文件落盘且保留子目录）/ 失败回退（url=/files/...）/ 未配置目录回退；files_generated 事件 payload 含新字段
  - [x] 新增 `tests/test_persist_files_download.py`：200 下载（含中文名）、404、403 穿越、mode=inline 行为
  - [x] 新增/扩展 `get_session_detail` 测试：upload_files 字段（含未消费记录 null 值）、messages/files 新字段透出
  - [x] 修正 `tests/test_upload_inject_and_bind.py`：append_messages mock 返回 dict、bind_message_id 新签名断言

- [x] Task 10: 验证
  - [x] `python -m py_compile` 通过全部修改文件
  - [x] 全量回归 `pytest`，全部通过

# Task Dependencies
- Task 1、2 无依赖，可并行
- Task 3、4 依赖 Task 1
- Task 5 依赖 Task 1
- Task 6 依赖 Task 3、4、5
- Task 7 依赖 Task 2、3（chat_service 直接依赖 DAO 新返回值与持久化目录配置；与 Task 6 可并行）
- Task 8 依赖 Task 2
- Task 9 依赖 Task 3-8
- Task 10 依赖 Task 9
