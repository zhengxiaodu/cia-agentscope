# 消息分享 + 收藏 + 上传文件管理接口 改造计划

## Summary

六个改造点，两张新表、两个新路由文件、两个新 DAO：

1. **消息分享**：`POST /message_share`（session_id + message_pair_ids 列表 → 16 位随机 shared_id，存新表 message_shares）
2. **分享查看**：`GET /message_share/{shared_id}`（**无需登录**，凭 shared_id 找到会话与消息 pair，经 get_session_detail 取详情并按 message_pair_id 过滤）
3. **/uploads 补字段**：每条返回 `upload_file_id`
4. **上传记录删除**：`DELETE /uploads?upload_file_id=`（不传则删该用户全部上传记录）
5. **收藏/取消收藏**：`POST /message_favorite`（按 message_pair_ids 批量复制消息到新表 message_favorites，**一批一个 favorite_id**）、`DELETE /message_favorite/{favorite_id}`
6. **收藏详情**：`GET /message_favorites`（返回用户全部收藏，按 favorite_id 分组）

## Current State Analysis

- **upload_files 表/DAO**（[upload_file_dao.py](file:///workspace/app/dao/upload_file_dao.py)）：`list_files_by_user` 目前 SELECT 不含 id（L166），返回键 session_id/message_id/filename/media_type/file_size；无删除方法
- **/uploads 路由**（[upload.py](file:///workspace/app/routes/upload.py#L79-L95)）：直接调 `request.app.state.upload_file_dao`（routes→DAO 直连的既有先例）
- **get_session_detail**（[session_service.py](file:///workspace/app/services/session_service.py#L107-L146)）：`meta["user_id"] != user_id` 时 raise PermissionError——分享查看需传**分享者的 user_id**（存于分享表）才能通过属主校验；返回 SessionDetailResponse（messages/files/upload_files 三个列表，各元素均带 message_pair_id）
- **messages 表结构**（[init_mysql.py](file:///workspace/app/dao/init_mysql.py#L36-L50)）：id/session_id/role/content/timestamp/agent_ids/user_id/success/tokens/message_pair_id/citations/bocha_sum
- **messages.user_id** 每轮落库均写入，可作为收藏复制时的属主过滤条件
- **路由注册**（[main.py](file:///workspace/app/main.py#L195-L204)）：lifespan 中创建 DAO 挂 app.state + include_router 模式
- **鉴权**：全部接口 `Depends(current_user)`；本次 GET 分享按用户确认**不挂鉴权**
- **建表**：INIT_SQL 幂等（CREATE TABLE IF NOT EXISTS + 索引容忍已存在错误码 1060/1061/1064）

## Proposed Changes

### 1. 新表 DDL（[init_mysql.py](file:///workspace/app/dao/init_mysql.py) INIT_SQL 追加）

```sql
-- 消息分享记录：shared_id 为 16 位随机十六进制串；不设 FK（分享记录独立于会话生命周期）
CREATE TABLE IF NOT EXISTS message_shares (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    shared_id        VARCHAR(32) NOT NULL,
    session_id       VARCHAR(64) NOT NULL,
    user_id          VARCHAR(64) NOT NULL DEFAULT '',
    message_pair_ids JSON NOT NULL,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE UNIQUE INDEX uniq_message_shares_shared_id ON message_shares(shared_id);
CREATE INDEX idx_message_shares_session_id ON message_shares(session_id);

-- 收藏消息：表结构与 messages 相同，仅新增 favorite_id（一批收藏共享一个 favorite_id）
-- 不设 FK 到 sessions（收藏是消息副本，会话删除后保留）；自增 id 即收藏时间顺序
CREATE TABLE IF NOT EXISTS message_favorites (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    favorite_id     VARCHAR(32) NOT NULL,
    session_id      VARCHAR(64) NOT NULL,
    role            VARCHAR(32) NOT NULL,
    content         TEXT NOT NULL,
    timestamp       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    agent_ids       JSON NULL DEFAULT NULL,
    user_id         VARCHAR(64) NOT NULL DEFAULT '',
    success         TINYINT(1) NOT NULL DEFAULT 1,
    tokens          INT NOT NULL DEFAULT 0,
    message_pair_id VARCHAR(64) NULL DEFAULT NULL,
    citations       JSON NULL,
    bocha_sum       JSON NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_message_favorites_user_fid ON message_favorites(user_id, favorite_id);
```

### 2. MessageShareDAO（新建 app/dao/message_share_dao.py）

```python
class MessageShareDAO:
    async def create_share(self, session_id, user_id, message_pair_ids: list[str]) -> str:
        """内部生成 shared_id（secrets.token_hex(8)，16 位十六进制小写）；
        INSERT 命中唯一键冲突（pymysql err 1062）时换 id 重试，最多 3 次；返回 shared_id。"""

    async def get_share(self, shared_id) -> Optional[dict]:
        """返回 {shared_id, session_id, user_id, message_pair_ids: list, created_at}；
        message_pair_ids 经 _parse_json_list 风格解析（str→json.loads 兜底 []）。"""
```

### 3. MessageFavoriteDAO（新建 app/dao/message_favorite_dao.py）

```python
class MessageFavoriteDAO:
    async def create_favorite(self, user_id, message_pair_ids: list[str]) -> tuple[str, int]:
        """生成 favorite_id（token_hex(8)），单语句 INSERT...SELECT 原子复制：
        INSERT INTO message_favorites
          (favorite_id, session_id, role, content, `timestamp`, agent_ids, user_id,
           success, tokens, message_pair_id, citations, bocha_sum)
        SELECT %s, m.session_id, m.role, m.content, m.`timestamp`, m.agent_ids, m.user_id,
               m.success, m.tokens, m.message_pair_id, m.citations, m.bocha_sum
        FROM messages m
        WHERE m.user_id = %s AND m.message_pair_id IN (%s,...)
        ORDER BY m.id ASC
        返回 (favorite_id, 复制行数)；ORDER BY 保证组内 user→assistant 顺序。"""

    async def delete_favorite(self, user_id, favorite_id) -> int:
        """DELETE WHERE user_id=%s AND favorite_id=%s，返回删除行数。"""

    async def list_favorites(self, user_id) -> list[dict]:
        """SELECT favorite_id, session_id, role, content, timestamp, agent_ids,
        user_id, success, tokens, message_pair_id, citations, bocha_sum
        WHERE user_id=%s ORDER BY id ASC；timestamp/JSON 列格式化对齐
        mysql_session_dao.load_messages 的返回风格。"""
```

### 4. 分享路由（新建 app/routes/message_share.py，本文件自带 success/error_response，同 sessions.py）

```python
@router.post("/message_share")           # 需登录
async def create_message_share(request, user=Depends(current_user)):
    body: {"session_id": str, "message_pair_ids": list[str]}
    # 校验：session_id 非空；message_pair_ids strip+去空+去重保序后非空
    # 属主校验：session_dao.get_session_meta(session_id)
    #   不存在 → 404 "会话不存在"；meta["user_id"] != user → 403 "会话不属于当前用户"
    # dao.create_share → {"shared_id": ...}

@router.get("/message_share/{shared_id}")  # 无需登录（不挂 current_user）
async def get_message_share(shared_id, request):
    # dao.get_share → None 时 404 "分享不存在"
    # session_service.get_session_detail(session_id, share["user_id"])  # 用分享者 id 过属主校验
    #   None → 404 "会话不存在或已删除"
    # 过滤：detail.messages / files / upload_files 三列表均保留
    #   message_pair_id ∈ share["message_pair_ids"] 的元素
    # 返回：detail.model_dump(mode="json") 字段 + {"shared_id": ...}
```

### 5. 收藏路由（新建 app/routes/message_favorite.py）

```python
@router.post("/message_favorite")          # 需登录
async def create_favorite(request, user=Depends(current_user)):
    body: {"message_pair_ids": list[str]}   # 一个或多个
    # 校验同上（非空、去重）；dao.create_favorite → 复制行数 0 → 404 "未找到对应消息"
    # 返回 {"favorite_id": ..., "count": n}

@router.delete("/message_favorite/{favorite_id}")   # 需登录
    # dao.delete_favorite → 0 → 404 "收藏不存在"；返回 {"deleted": n}

@router.get("/message_favorites")          # 需登录
    # dao.list_favorites(user_id) → Python 按 favorite_id 分组（dict 插入序 = 收藏时间序）
    # 返回 {"favorites": [{"favorite_id": ..., "messages": [消息dict, ...]}, ...]}
    # 每条消息含 role/content/timestamp/agent_ids/user_id/success/tokens/
    # message_pair_id/citations/bocha_sum/session_id（跨会话收藏时前端可区分来源）
```

### 6. /uploads 补 upload_file_id（[upload_file_dao.py](file:///workspace/app/dao/upload_file_dao.py) L157-172）

`list_files_by_user` 的 SELECT 增加 `id`，返回 dict 增加 `"upload_file_id": r["id"]`，其余键名不变（向后兼容）。

### 7. 上传记录删除（[upload.py](file:///workspace/app/routes/upload.py) + upload_file_dao.py）

```python
@router.delete("/uploads")     # 需登录
async def delete_user_uploads(request, upload_file_id: Optional[int] = Query(None), user=...):
    # 传 upload_file_id：dao.delete_by_id(user_id, file_id)  # WHERE id=%s AND user_id=%s
    #   rowcount=0 → 404 "文件不存在"；返回 {"deleted": 1}
    # 不传：dao.delete_all_by_user(user_id)  # WHERE user_id=%s；返回 {"deleted": n}
```

upload_file_dao.py 新增 `delete_by_id` / `delete_all_by_user` 两个方法（物理文件无需清理：现架构上传文件不落盘，解析内容在库内）。

### 8. main.py 接线（[main.py](file:///workspace/app/main.py)）

- lifespan：`app.state.message_share_dao = MessageShareDAO(mysql_pool)`、`app.state.message_favorite_dao = MessageFavoriteDAO(mysql_pool)`（紧随 UploadFileDAO 之后）
- `include_router(message_share.router, tags=["message-share"])`、`include_router(message_favorite.router, tags=["message-favorite"])`；routes import 列表同步

### 9. 测试（新建 3 个文件，复用 fake pool/cursor + TestClient 模式）

- **tests/test_message_share.py**：DAO（create_share SQL/参数、1062 冲突重试一次后成功、get_share 解析）；POST（空 pair 列表 400、会话不存在 404、非属主 403、成功返回 shared_id）；GET（**不带 JWT 返回 200**、shared_id 无效 404、三列表均按 pair 过滤、不在列表内的消息被剔除）
- **tests/test_message_favorite.py**：DAO（INSERT...SELECT 的 SQL 含 ORDER BY m.id、IN 占位符与参数、复制行数；delete；list 的 timestamp/JSON 格式化）；POST（空列表 400、0 行 404、成功返回 favorite_id+count）；DELETE（404/成功）；GET（多组分组正确、组内顺序 user→assistant、组间按收藏先后）
- **tests/test_uploads_list_delete.py**：list 返回含 upload_file_id 且其余键不变；DELETE by id（404/成功）、DELETE all（返回删除数）

## Assumptions & Decisions

1. **shared_id / favorite_id 均为 `secrets.token_hex(8)`（16 位十六进制小写）**；shared_id 有 UNIQUE 索引+重试，favorite_id 靠 64 位随机性（同用户碰撞概率可忽略）
2. **GET /message_share/{shared_id} 无需登录**（用户确认）；POST 分享/收藏相关接口均需登录
3. **favorite_id 一批一个**（用户确认）：一次请求的多个 message_pair_id 归入同一收藏组；取消收藏按 favorite_id 删除整组
4. **收藏表严格 = messages 列 + favorite_id**（用户要求），不加 created_at 等额外字段；收藏先后顺序用自增 id 表达
5. **分享详情复用 get_session_detail**（用户指定），传分享表中的 user_id 通过属主校验；messages/files/upload_files 三列表统一按 message_pair_id 过滤（"筛选包含输入消息ID的部分结果"）
6. **收藏复制用 INSERT...SELECT 单语句**：原子、不经过 Python 中转、保留原 timestamp 字面值；ORDER BY m.id 保证组内顺序
7. 两张新表均**不设外键**（记录独立于会话生命周期：会话删除后分享返回 404、收藏保留）
8. DELETE /uploads 不传 id 时**直接全删**该用户上传记录（按用户要求，无二次确认）；仅删 DB 行，无物理文件清理
9. message_pair_ids 入参统一 strip、去空、去重保序；`None`/非列表/空列表 → 400
10. POST /message_share 校验会话属主（防越权分享他人会话）；GET 分享不校验查看者身份（分享语义）

## Verification

1. `python -m py_compile app/dao/message_share_dao.py app/dao/message_favorite_dao.py app/routes/message_share.py app/routes/message_favorite.py app/routes/upload.py app/dao/upload_file_dao.py app/dao/init_mysql.py app/main.py`
2. `python -m pytest tests/test_message_share.py tests/test_message_favorite.py tests/test_uploads_list_delete.py -v` 全绿
3. `python -m pytest tests/ -q` 全量回归（已知 2 个 regulations dashboard 401 存量失败与本次无关）
4. 手动验证（可选）：启动后 `curl POST /message_share` → 拿 shared_id → 未带 JWT `curl GET /message_share/{shared_id}` 看过滤结果；`curl POST /message_favorite` → `GET /message_favorites` 看分组；`DELETE /uploads?upload_file_id=1` 与 `DELETE /uploads`
