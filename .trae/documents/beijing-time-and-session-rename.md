# 时间字段统一北京时间 + 新增修改会话名称接口 改造计划

## Summary

1. **时间字段统一北京时间**：当前数据库时间写入有两套口径——Python 侧 `datetime.now(timezone.utc)`（UTC，比北京时间早 8 小时）和 SQL 侧 `NOW()`/`DEFAULT CURRENT_TIMESTAMP`（MySQL 宿主机时区）。统一为北京时间：Python 侧改用东八区生成时间，MySQL 连接池固定会话时区 `+08:00`（使 NOW()/CURRENT_TIMESTAMP 与 Python 侧对齐，且不依赖宿主机时区）。
2. **新增修改会话名称接口**：`PUT /sessions/{session_id}/name`，输入 session_id 与新名称，更新 sessions 表 name 字段（对标现有 pin/unpin 三层模式）。

## Current State Analysis

### 时间字段现状（问题根源：两套口径混用）

| 写入方式 | 位置 | 时间口径 |
|---|---|---|
| Python `datetime.now(timezone.utc)` | [mysql_session_dao.py](file:///workspace/app/dao/mysql_session_dao.py) L94（save_agent_state）、L194（append_messages）、L509（pin_session）、L672（update_session_state）；[chat_service.py:230](file:///workspace/app/services/chat_service.py#L230)（消息 timestamp 字符串） | **UTC**（早 8 小时） |
| Python `.replace(tzinfo=timezone.utc)` | mysql_session_dao.py L245（消息 timestamp 字符串解析） | UTC 语义 |
| SQL `NOW()` | mysql_session_dao.py L525（unpin）、L349（save_latest_trace_id）；[upload_file_dao.py](file:///workspace/app/dao/upload_file_dao.py) L55/L74/L132 | MySQL 宿主机时区（未显式设置） |
| `DEFAULT CURRENT_TIMESTAMP` | init_mysql.py 各表（INSERT 未显式传时间时） | MySQL 宿主机时区 |

[main.py:102-119](file:///workspace/app/main.py#L102-L119) `aiomysql.create_pool` 未设置任何时区参数。同一张表里 Python 写的行比 NOW() 写的行字面时间早 8 小时，会话列表/历史详情时间不一致。

项目已有东八区先例：[rewriter.py:14](file:///workspace/app/intent/rewriter.py#L14) `_SHANGHAI_TZ = timezone(timedelta(hours=8))`。

### 会话更新接口现状

pin/unpin 三层模式（本次 rename 完全对标）：
- 路由：[sessions.py:46-59](file:///workspace/app/routes/sessions.py#L46-L59) `PUT /sessions/{session_id}/pin`（path 参数 + body）
- 服务：[session_service.py:76-82](file:///workspace/app/services/session_service.py#L76-L82) 透传
- DAO：[mysql_session_dao.py:507-528](file:///workspace/app/dao/mysql_session_dao.py#L507-L528) `UPDATE sessions ... WHERE session_id = %s AND user_id = %s`

sessions.name 为 `VARCHAR(255) NOT NULL DEFAULT ''`（init_mysql.py L12）。

## Proposed Changes

### 改造点 1：时间字段统一北京时间

#### 1a. [app/dao/mysql_session_dao.py](file:///workspace/app/dao/mysql_session_dao.py)

模块头部（现有 `from datetime import datetime, timezone` 处）：

```python
from datetime import datetime, timedelta, timezone

# 东八区（Asia/Shanghai）：所有表时间字段统一按北京时间写入
_BEIJING_TZ = timezone(timedelta(hours=8))
```

替换 4 处 `now = datetime.now(timezone.utc)` → `now = datetime.now(_BEIJING_TZ)`（L94、L194、L509、L672）；L245 `.replace(tzinfo=timezone.utc)` → `.replace(tzinfo=_BEIJING_TZ)`（chat_service 传入的 timestamp 字符串此后为北京时间字符串）。

#### 1b. [app/services/chat_service.py](file:///workspace/app/services/chat_service.py) L230

消息 timestamp 字符串生成处：

```python
now_str = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
```

在 chat_service.py 模块头部同样定义 `_BEIJING_TZ = timezone(timedelta(hours=8))`（需补 `timedelta` import；沿用 rewriter.py 各自定义的现有模式，不抽公共模块）。

#### 1c. [app/main.py](file:///workspace/app/main.py) L102-119 MySQL 连接池

`aiomysql.create_pool` 增加 `init_command` 参数：

```python
pool = await aiomysql.create_pool(
    ...,
    autocommit=True,
    init_command="SET time_zone = '+08:00'",
)
```

作用：固定每个连接的会话时区为东八区——SQL 侧 `NOW()` / `DEFAULT CURRENT_TIMESTAMP` 一律返回北京时间；TIMESTAMP 列的读写字面值均按 +08:00 解释，与 Python 侧写入完全对齐（历史数据字面值读出不变，不做数据刷新）。

#### 1d. 不改的部分（明确排除）

- [upload_file_dao.py](file:///workspace/app/dao/upload_file_dao.py) 的 `NOW()`、INSERT 依赖 DEFAULT：会话时区固定后自动正确
- [session_dao.py](file:///workspace/app/dao/session_dao.py)（Redis 遗留，无引用）
- [auth_service.py](file:///workspace/app/services/auth_service.py) L19/L33（JWT epoch 时间戳，非表字段）
- [logging_setup.py](file:///workspace/app/utils/logging_setup.py)（日志格式化，惯例用 UTC）
- [dashboard_service.py:258](file:///workspace/app/regulations/services/dashboard_service.py#L258) / [dashboard_aggregation.py:107](file:///workspace/app/regulations/services/dashboard_aggregation.py#L107)（Langfuse API 查询时间范围 / 趋势分组，非表插入）
- 历史存量数据不刷新（字面值读出不变，仅新写入统一）

### 改造点 2：修改会话名称接口

#### 2a. DAO 层 [mysql_session_dao.py](file:///workspace/app/dao/mysql_session_dao.py)

在 unpin_session 之后新增：

```python
async def rename_session(
    self, user_id: str, session_id: str, name: str
) -> bool:
    """修改会话名称。返回 False 表示会话不存在（或不属于该用户）。

    不更新 updated_at：改名不应改变会话在列表中的排序。
    """
    async with self.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "UPDATE sessions SET name = %s "
                "WHERE session_id = %s AND user_id = %s",
                (name, session_id, user_id),
            )
            await conn.commit()
            return cur.rowcount > 0
```

#### 2b. 服务层 [session_service.py](file:///workspace/app/services/session_service.py)

在 unpin_session 之后新增透传：

```python
async def rename_session(
    self, user_id: str, session_id: str, name: str
) -> bool:
    """修改会话名称。返回 False 表示会话不存在。"""
    return await self.dao.rename_session(user_id, session_id, name)
```

#### 2c. 路由层 [sessions.py](file:///workspace/app/routes/sessions.py)

在 pin 接口之后新增：

```python
@router.put("/sessions/{session_id}/name")
async def rename_session(
    session_id: str,
    request: Request,
    user: dict = Depends(current_user),
):
    service = _get_session_service(request)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return error_response(400, "会话名称不能为空")
    if len(name) > 255:
        return error_response(400, "会话名称不能超过255个字符")
    ok = await service.rename_session(user.get("user_id"), session_id, name)
    if not ok:
        return error_response(404, "会话不存在")
    return success_response({"session_id": session_id, "name": name})
```

### 改造点 3：测试

#### 3a. 新增 tests/test_session_rename.py

复用 test_sessions_pagination.py 的模式（自建最小 app + 真 JWT + mock session_service；DAO 用假 pool/cursor）：

- **DAO**：`rename_session` SQL 为 `UPDATE sessions SET name = %s WHERE session_id = %s AND user_id = %s`；参数正确；rowcount=1 → True、rowcount=0 → False；SQL 不含 updated_at
- **路由**：
  - 正常改名 → 200，返回 `{session_id, name}`，service 收到 strip 后的名称
  - name 为空/全空格/缺失 → 400
  - name 超 255 字符 → 400
  - 会话不存在（service 返回 False）→ 404
  - 无 JWT → 401

#### 3b. 新增 tests/test_beijing_time_writes.py

- **DAO 写入时间为北京时间**：假 pool/cursor 捕获 `pin_session` 的 SQL 参数，断言 `now.utcoffset() == timedelta(hours=8)` 且与 `datetime.now(东八区)` 字面值差 < 2 秒（save_agent_state/append_messages 同模式，抽一个捕获 helper）
- **chat_service 消息 timestamp 字符串**：构造 _persist_conversation_history 场景（或直接调用其时间构造逻辑），解析 now_str 为 naive datetime，与 `datetime.now(东八区)` 差 < 2 秒（允许执行耗时）
- **main.py 连接池参数**：读 main.py 源码断言含 `init_command` 且值为 `SET time_zone = '+08:00'`（静态断言，不真实连库）

## Assumptions & Decisions

1. **统一策略 = Python 东八区 + 会话时区固定 +08:00**：只改 Python 侧而不固定会话时区的话，NOW() 仍依赖宿主机时区，跨环境部署不可控；固定后两边严格对齐。
2. **改名不更新 updated_at**：会话列表按 `updated_at DESC` 排序，改名不应使会话跳到顶部（对标主流聊天产品交互）。
3. **接口形态**：`PUT /sessions/{session_id}/name` + body `{"name": "..."}`，与 pin 接口的 path+body 模式一致；名称 strip 后 1~255 字符（对齐 VARCHAR(255)）。
4. **历史存量数据不刷新**：本次只统一新写入；TIMESTAMP 列按会话时区转换，历史行读出字面值不变。
5. **JWT/日志/Langfuse 查询时间不属于"表时间字段"**，不改。
6. 权限语义与 pin 一致：`WHERE user_id = %s` 保证只能改自己的会话（不属于自己的返回 404，不暴露存在性）。

## Verification

1. `python -m py_compile app/dao/mysql_session_dao.py app/services/session_service.py app/services/chat_service.py app/routes/sessions.py app/main.py`
2. `python -m pytest tests/test_session_rename.py tests/test_beijing_time_writes.py -v` 新增测试全绿
3. `python -m pytest tests/ -q` 全量回归（重点关注既有测试是否断言了 UTC 时间需同步）
4. 手动验证（可选）：
   - `curl -X PUT .../sessions/{id}/name -d '{"name":"新名字"}'` → 200，GET /sessions 中名称已更新
   - 新建会话发消息后查库：`SELECT created_at, updated_at FROM sessions` 与北京时间手表一致
