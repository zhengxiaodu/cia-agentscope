# 计划：按用户查询上传文件接口 + 登录返回 agent_access 格式调整

## Summary

1. **新增文件查询接口**：`GET /uploads?user_id=xxx`，返回该用户上传过的文件列表（session_id、message_id、文件名、文件类型、文件大小）。前置改造：`upload_files` 表补 `user_id`、`file_size` 两列（现表两者皆无），上传链路写入这两个字段，并幂等回填存量数据。
2. **登录返回格式调整**：`agent_access` 中缺 description 的项补空字符串 `""`；返回前端时去掉每项的 `show` 字段。仅影响响应构造，Redis 存储的原始权限不变。

## Current State Analysis

- [upload_files 表](file:///workspace/app/dao/init_mysql.py#L70-L84)：字段仅 id/session_id/filename/media_type/parse_type/status/parsed_content/error_message/message_id/created_at/updated_at，**无 user_id、无 file_size**。
- [UploadFileDAO.insert](file:///workspace/app/dao/upload_file_dao.py#L22-L41)：签名 `(session_id, filename, media_type, parse_type)`，不含 user_id 与大小。
- [start_background_parse](file:///workspace/app/services/file_parse_service.py#L246-L265)：参数无 user_id；content（文件字节）已在上传处读入，size 可用 `len(content)` 得出。
- [upload.py](file:///workspace/app/routes/upload.py#L49-L58)：已有 `user_id = user.get("user_id")`，但未传给 start_background_parse。
- [auth.py 响应构造](file:///workspace/app/routes/auth.py)：三处返回 `agent_access`（`_build_auth_success` L188、`_build_update_response` L263、`refresh_token` L338），均为 `_merge_regulations_agents(permissions[...])` 输出；项结构含 `show`，description 由 `_enrich_agent_access` 注入（未匹配则无该字段）。
- 受影响测试：[test_file_parse_service.py L262](file:///workspace/tests/test_file_parse_service.py#L262) 断言 insert 参数元组；[test_agent_access_description.py](file:///workspace/tests/test_agent_access_description.py) 断言 `"description" not in ...`（补空串后语义变化）。

## Proposed Changes

### 需求 1：按 user_id 查询上传文件

**1. [app/dao/init_mysql.py](file:///workspace/app/dao/init_mysql.py)**
- `upload_files` 建表 DDL 增加：`user_id VARCHAR(64) NOT NULL DEFAULT ''`、`file_size BIGINT NOT NULL DEFAULT 0`（新环境直接建全）。
- 文件末尾兼容区（既有 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 模式）追加：
  - `ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS (user_id VARCHAR(64) NOT NULL DEFAULT '');`
  - `ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS (file_size BIGINT NOT NULL DEFAULT 0);`
- 新增查询索引：`CREATE INDEX idx_upload_files_user_id ON upload_files(user_id);`（重复创建走既有 1061 错误码忽略逻辑，幂等）。
- 存量回填（幂等，放 INIT_SQL 末尾）：`UPDATE upload_files uf JOIN sessions s ON uf.session_id = s.session_id SET uf.user_id = s.user_id WHERE uf.user_id = '';`——session 已删除的孤儿记录保持 `''`，不会误归属。

**2. [app/dao/upload_file_dao.py](file:///workspace/app/dao/upload_file_dao.py)**
- `insert` 签名改为 `(session_id, user_id, filename, media_type, parse_type, file_size)`，INSERT 语句同步加两列。
- 新增 `list_files_by_user(user_id: str) -> List[dict]`：
  ```sql
  SELECT session_id, message_id, filename, media_type, file_size
  FROM upload_files WHERE user_id = %s ORDER BY id DESC
  ```
  （最新在前；message_id 未绑定为 NULL 原样返回；风格对齐现有 load_unbound_parsed）

**3. [app/services/file_parse_service.py](file:///workspace/app/services/file_parse_service.py)**
- `start_background_parse` 加参数 `user_id: str = ""`，insert 调用改为 `dao.insert(session_id, user_id, filename, media_type, parse_type, len(content))`。

**4. [app/routes/upload.py](file:///workspace/app/routes/upload.py)**
- 调用 `start_background_parse` 时传入 `user_id=user_id`。
- 新增路由：
  ```python
  @router.get("/uploads")
  async def list_user_uploads(request: Request, user_id: str, user: dict = Depends(current_user)):
  ```
  - `user_id` 为必填 query 参数，缺失由 FastAPI 返回 422（不额外手写校验）。
  - DAO 取自 `request.app.state.upload_file_dao`，为 None 时 500。
  - 返回 `{"code": 200, "msg": "success", "data": {"files": [...]}}`（对齐 sessions.py 风格，非 UploadResponse 模型）。
  - 鉴权走统一 JWT（current_user），查询目标 user_id 以请求参数为准（管理端可查任意用户）。

### 需求 2：agent_access 格式调整

**5. [app/routes/auth.py](file:///workspace/app/routes/auth.py)**
- 新增 `_normalize_agent_access(agent_access: list) -> list`：
  - 每个非 dict 项原样保留（健壮性，与 `_merge_regulations_agents` 一致）；
  - dict 项浅拷贝后：`pop("show", None)`；`item["description"] = item.get("description") or ""`（None/缺失均兜底空串）；
  - 其余字段原样保留。
- 三处响应点统一改为 `_normalize_agent_access(_merge_regulations_agents(...))`：
  - `_build_auth_success`（login/register）
  - `_build_update_response`（update name/department/password）
  - `refresh_token`
- **不改动**：`_merge_regulations_agents`、`_enrich_agent_access`、Redis permissions 存储内容（存的是 enrich 后未合并未 normalize 的原始结构）、`_OPTIONAL_SKILLS`、`user_info`。

### 测试

**6. 更新 [tests/test_file_parse_service.py](file:///workspace/tests/test_file_parse_service.py)**
- L262 断言改为含 `user_id` 与 `file_size` 的参数元组（`start_background_parse` 调用同步传 `user_id="u1"` 类参数）。

**7. 更新 [tests/test_agent_access_description.py](file:///workspace/tests/test_agent_access_description.py)**
- `test_degrade_when_orchestrator_missing` / `test_degrade_when_fuse_fails` 中 `"description" not in by_id["999"]` 改为 `by_id["999"]["description"] == ""`。
- 集成测试补断言：`agent_access` 各项 `"show" not in a`；Redis 中保存的原始 permissions 仍含 show、无空串 description（不变性验证）。
- 新增 `TestNormalizeAgentAccess` 类：补空串、去 show、保留其他字段、非 dict 项健壮性、不修改入参。

**8. 新增 tests/test_upload_query.py**
- DAO：假 pool/cursor（风格对齐 test_regulations_knowledge_gap 的 DAO 测试）验证 SQL 参数与行映射、按 user_id 过滤。
- 路由：TestClient 最小 app——401（无 JWT）、200 结构（files 列表字段齐全）、message_id 为 None 的行原样返回。

## Assumptions & Decisions

- `file_size` = 上传时 `len(content)`（字节），不从 Content-Length 头取。
- 存量数据回填通过 INIT_SQL 幂等 UPDATE（JOIN sessions），孤儿记录留空不影响查询正确性。
- 接口路径定 `GET /uploads`（与 POST /upload 同资源域，放同一文件）；user_id 作为必填 query 参数，鉴权仍统一 JWT。
- 不做分页（用户未要求，列表按 id DESC 返回全部）。
- normalize 只在"返回前端"层做，Redis/下游（policy_qa 权限映射、merge 逻辑）全部不受影响。

## Verification

1. `cd /workspace && python -m pytest tests/ -q` 全量通过（334+ 基线 + 新增/更新用例）。
2. `python -c "import app.main"` 可导入。
3. 手工核验（如有 MySQL 环境）：重复执行 init（幂等不报错）；`GET /uploads` 无 JWT → 401、带 JWT → 200 且字段符合契约。
