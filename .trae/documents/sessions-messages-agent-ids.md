# sessions/messages 表新增 agent_ids 字段

## 背景与目标

当前持久化保存对话历史时，无法区分某条 assistant 消息是由哪个/哪些 agent 生成的。需要在 `sessions` 和 `messages` 两表各加一个 `agent_ids` 字段（JSON 列表），用于记录参与生成该消息的 agent_id 列表；sessions 表则累积去重记录该会话历史中用过的所有 agent_id。

## 当前状态分析

### 表结构（[init_mysql.py](file:///workspace/app/dao/init_mysql.py)）
- `sessions` 表（行 9-19）：无 agent_ids 字段
- `messages` 表（行 35-42）：字段为 `id/session_id/role/content/timestamp`，无 agent_ids 字段

### 持久化链路
1. [chat_service.py:271-274](file:///workspace/app/services/chat_service.py#L271-L274) 调 `session_service.append_messages(session_id, user_id, new_messages)`，`new_messages` 每条是 `{"role","content","timestamp"}` dict
2. [session_service.py:37-41](file:///workspace/app/services/session_service.py#L37-L41) 透传到 DAO
3. [mysql_session_dao.py:146-241](file:///workspace/app/dao/mysql_session_dao.py#L146-L241) `append_messages()`：
   - 行 198-204 INSERT messages 用 `(session_id, role, content, timestamp)`
   - 行 207-236 更新 sessions 元信息（name/message_count/updated_at）

### agent_id 来源（orchestrator_service.py）
- **多 agent 路径**（行 600-622）：`orchestrator._last_results` 含每个 `r.agent_id`；`self._last_orchestrator = orchestrator`（行 609）被设置
- **单 agent 路径**（行 448-533）：用入参 `agent_id`，**不设置 `_last_orchestrator`**，因此 `last_agent_states` 属性（行 155-167）无法覆盖单 agent 路径
- [chat_service.py:102-112](file:///workspace/app/services/chat_service.py#L102-L112) `generate_response()` 入参有 `agent_id: Optional[str]`，但多 agent 路径下为 None

### 读取链路
- [mysql_session_dao.py:122-144](file:///workspace/app/dao/mysql_session_dao.py#L122-L144) `load_messages()` SELECT `role, content, timestamp`，返回 dict 列表
- [mysql_session_dao.py:264-291](file:///workspace/app/dao/mysql_session_dao.py#L264-L291) `get_session_meta()` SELECT 各字段
- [mysql_session_dao.py:297-376](file:///workspace/app/dao/mysql_session_dao.py#L297-L376) `list_user_sessions()` SELECT 各字段
- [models/session.py](file:///workspace/app/models/session.py) `SessionMessage`/`SessionMeta` 无 agent_ids 字段

## 设计决策（已与用户确认）

1. **存储格式**：JSON 列表。messages 表 `agent_ids` JSON 列存 `["a1","a2"]`；user 消息为 `[]`，assistant 消息记录生成它的 agent 列表。sessions 表 `agent_ids` JSON 列累积去重所有。
2. **会话级语义**：sessions.agent_ids 累积去重所有用过的 agent_id，每次 append_messages 时合并更新。
3. **agent_id 来源统一**：在 OrchestratorService 新增 `last_agent_ids` 属性，覆盖单 agent 路径（在单 agent 路径设置 `self._last_agent_ids = [agent_id]`）和多 agent 路径（从 `_last_orchestrator._last_results` 提取）。多 agent 路径执行前重置为 `[]`。
4. **chat_service 传参**：在 `new_messages` 的 assistant 消息 dict 中加 `"agent_ids": [...]`；user 消息加 `"agent_ids": []`。`append_messages` 签名不变，仍接收 dict 列表，DAO 从 dict 中读 `agent_ids`。
5. **兼容旧数据**：新增列用 `JSON NULL DEFAULT NULL`，旧数据该字段为 NULL；读取时 DAO 用 `if row.get("agent_ids") is None: []` 兜底。
6. **建表迁移**：`init_mysql.py` 的 `CREATE TABLE` 加列（新部署幂等）；对已部署环境，在 `init_mysql_tables` 中追加 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 兼容（MySQL 8 支持 IF NOT EXISTS，若为 MySQL 5.7 用 try/except 忽略 1060 重复列错误）。

## 改动清单

### 1. 修改 `/workspace/app/dao/init_mysql.py`

**sessions 表 CREATE TABLE 加列**（行 17 `is_pinned` 后、行 18 `pinned_at` 前）：
```sql
    agent_ids    JSON NULL DEFAULT NULL,
```

**messages 表 CREATE TABLE 加列**（行 40 `timestamp` 后）：
```sql
    agent_ids    JSON NULL DEFAULT NULL,
```

**在 INIT_SQL 末尾追加 ALTER 兼容语句**（用于已存在表的迁移）：
```sql
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS (agent_ids JSON NULL DEFAULT NULL);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS (agent_ids JSON NULL DEFAULT NULL);
```

注意：`init_mysql_tables` 内的 try/except（行 91-101）已处理错误码；MySQL 5.7 不支持 `ADD COLUMN IF NOT EXISTS` 会抛 1064，需扩展 except 处理。但若部署环境为 MySQL 8（docker-compose.yml 默认），`IF NOT EXISTS` 直接生效。为稳妥，扩展 except 同时忽略 1060（Duplicate column）与 1064。

### 2. 修改 `/workspace/app/services/orchestrator_service.py`

**新增属性 `last_agent_ids`**（在 `last_agent_states` 属性后，约行 168）：
```python
@property
def last_agent_ids(self) -> List[str]:
    """获取最近一次编排中参与的 agent_id 列表（去重）。

    覆盖单 agent 直接问答与多 agent 编排两条路径。
    """
    return list(self._last_agent_ids) if self._last_agent_ids else []
```

**在 `__init__` 初始化字段**（约行 87 `self._last_orchestrator` 后）：
```python
self._last_agent_ids: List[str] = []
```

**单 agent 路径设置 agent_ids**（行 519 保存 AgentState 后、行 525 except 前）：
```python
# 记录本轮参与的 agent_id（供 chat_service 持久化到 messages）
self._last_agent_ids = [agent_id]
```

**多 agent 路径设置 agent_ids**（行 609 `self._last_orchestrator = orchestrator` 后）：
```python
# 汇总本轮编排参与的 agent_id 列表
self._last_agent_ids = [
    r.agent_id for r in orchestrator._last_results if r.agent_id
]
```

**多 agent 路径开始前重置**（行 600 `async for event_str in orchestrator.run(` 前）：
```python
self._last_agent_ids = []
```

需在文件顶部确认 `List` 已导入（行 14 `from typing import Any, AsyncGenerator, Dict, List, Optional` 已有）。

### 3. 修改 `/workspace/app/dao/mysql_session_dao.py`

**`append_messages()` 改造**（行 146-241）：

INSERT messages 时加 agent_ids 列（行 198-204）：
```python
for msg in new_messages:
    ts_raw = msg.get("timestamp", now_str)
    if isinstance(ts_raw, str):
        try:
            ts = datetime.strptime(
                ts_raw, "%Y-%m-%d %H:%M:%S.%f"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            ts = now
    else:
        ts = now
    agent_ids = msg.get("agent_ids", [])
    agent_ids_json = json.dumps(agent_ids, ensure_ascii=False) if agent_ids else None
    await cur.execute(
        "INSERT INTO messages "
        "(session_id, role, content, timestamp, agent_ids) "
        "VALUES (%s, %s, %s, %s, %s)",
        (session_id, msg.get("role", "user"),
         msg.get("content", ""), ts, agent_ids_json),
    )
```

INSERT sessions 时加 agent_ids（行 179-184 与行 224-229 两处 UPDATE sessions）：

新建 sessions 行时（行 179-184）：
```python
agent_ids_init = json.dumps(agent_ids_all, ensure_ascii=False) if agent_ids_all else None
await cur.execute(
    "INSERT INTO sessions "
    "(session_id, user_id, name, created_at, updated_at, agent_ids) "
    "VALUES (%s, %s, %s, %s, %s, %s)",
    (session_id, user_id, name, now, now, agent_ids_init),
)
```

其中 `agent_ids_all` 为本轮 new_messages 中所有 agent_ids 去重后的列表（新建会话时直接用）。

已存在 sessions 行时（行 207-236 的 UPDATE 块），需要先读旧 agent_ids 再合并去重：
```python
# 读取旧 agent_ids 并合并本轮新增的（去重）
await cur.execute(
    "SELECT agent_ids FROM sessions WHERE session_id = %s",
    (session_id,),
)
old_row = await cur.fetchone()
old_agent_ids = []
if old_row and old_row.get("agent_ids"):
    raw = old_row["agent_ids"]
    if isinstance(raw, str):
        try:
            old_agent_ids = json.loads(raw)
        except Exception:
            old_agent_ids = []
    elif isinstance(raw, list):
        old_agent_ids = raw
merged = list(dict.fromkeys(old_agent_ids + agent_ids_all))  # 去重保序
agent_ids_json = json.dumps(merged, ensure_ascii=False) if merged else None

# 然后执行 UPDATE sessions SET agent_ids = %s, ...
```

注意：原代码中"已存在 sessions 行"分支在行 207 之后有两处 UPDATE（行 224-229 新建 name 时 + 行 231-236 已有 name 时），都需要加 `agent_ids = %s`。

**`load_messages()` 改造**（行 122-144）：

SELECT 加 agent_ids，返回 dict 加 agent_ids 字段：
```python
await cur.execute(
    "SELECT role, content, timestamp, agent_ids FROM messages "
    "WHERE session_id = %s ORDER BY id ASC",
    (session_id,),
)
rows = await cur.fetchall()
return [
    {
        "role": r["role"],
        "content": r["content"],
        "timestamp": r["timestamp"].strftime(...) if hasattr(...) else str(...),
        "agent_ids": _parse_json_list(r.get("agent_ids")),
    }
    for r in rows
]
```

新增模块级辅助函数 `_parse_json_list`：
```python
def _parse_json_list(raw) -> list:
    """把 MySQL JSON 列返回值统一解析为 list。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except Exception:
            return []
    return []
```

**`get_session_meta()` 改造**（行 264-291）：SELECT 加 agent_ids，返回 dict 加 `agent_ids` 字段（用 `_parse_json_list`）。

**`list_user_sessions()` 改造**（行 297-376）：两处 SELECT（pinned 和非 pinned）都加 agent_ids；两处构造返回 dict 都加 `agent_ids` 字段。

**`get_session()` 改造**（行 504-523）：SELECT 已含 `s.*` 部分字段，需加 `s.agent_ids`；返回 dict 不需特殊处理（保持原样透传，但 agent_ids 是 str 需解析为 list）。

### 4. 修改 `/workspace/app/models/session.py`

**`SessionMessage` 加字段**（行 6-9）：
```python
class SessionMessage(BaseModel):
    role: str
    content: str
    timestamp: str
    agent_ids: List[str] = []
```

**`SessionMeta` 加字段**（行 21-27）：
```python
class SessionMeta(BaseModel):
    session_id: str
    user_id: str
    name: str = ""
    created_at: str
    updated_at: str
    message_count: int
    agent_ids: List[str] = []
```

### 5. 修改 `/workspace/app/services/chat_service.py`

**在持久化块（行 246-276）中，构造 new_messages 时加 agent_ids**：

在 `new_messages = []` 之前，先从 orchestrator_service 取本轮 agent_ids：
```python
involved_agent_ids = []
if orchestrator_service is not None:
    try:
        involved_agent_ids = orchestrator_service.last_agent_ids
    except Exception:
        involved_agent_ids = []
```

user 消息加 `"agent_ids": []`，assistant 消息加 `"agent_ids": involved_agent_ids`：
```python
new_messages = []
if user_input:
    new_messages.append({
        "role": "user", "content": user_input, "timestamp": now_str,
        "agent_ids": [],
    })
if final_output:
    new_messages.append({
        "role": "assistant", "content": final_output, "timestamp": now_str,
        "agent_ids": involved_agent_ids,
    })
```

注意：`orchestrator_service` 是 `generate_response` 的入参（行 102），已在作用域内。

### 6. 修改 `/workspace/app/services/session_service.py`

`SessionService.append_messages` 签名不变（行 37-41），透传 dict 列表给 DAO，DAO 从 dict 读 agent_ids。**无需改动**。

## 假设与边界

- 旧数据 agent_ids 为 NULL，读取时兜底为 `[]`
- user 消息 agent_ids 永远为 `[]`（不参与生成的消息无 agent）
- 单 agent 路径：`_last_agent_ids = [agent_id]`（即使 agent_id 来自前端指定）
- 多 agent 路径：从 `orchestrator._last_results` 提取，去重保序
- sessions.agent_ids 合并采用"读旧→合并→写新"，事务内完成
- `_parse_json_list` 兼容 aiomysql DictCursor 对 JSON 列的两种返回形态（str 或已解析 list，取决于驱动版本）
- `init_mysql.py` 的 ALTER 语句依赖 MySQL 8 的 `IF NOT EXISTS`；若环境为 MySQL 5.7 会抛 1064，扩展 except 忽略

## 验证步骤

1. **静态校验**：`python -m py_compile` 对 5 个改动文件
   - `app/dao/init_mysql.py`
   - `app/services/orchestrator_service.py`
   - `app/dao/mysql_session_dao.py`
   - `app/models/session.py`
   - `app/services/chat_service.py`
2. **grep 核对**：
   - `agent_ids` 在 5 个文件中均出现
   - `last_agent_ids` 在 orchestrator_service.py 中定义并被 chat_service.py 调用
   - `_parse_json_list` 在 mysql_session_dao.py 中定义
   - INIT_SQL 含 `agent_ids JSON` 与 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
3. **YAML/SQL 语法**：init_mysql.py 的 `_split_statements` 仍能正确拆分
4. **git 提交并 push** 到 `origin/trae/agent-5CYjia`

## 改动文件清单

- `/workspace/app/dao/init_mysql.py`（建表 SQL 加 agent_ids 列 + ALTER 兼容语句）
- `/workspace/app/services/orchestrator_service.py`（新增 last_agent_ids 属性 + 单/多 agent 路径设置 _last_agent_ids）
- `/workspace/app/dao/mysql_session_dao.py`（append_messages/load_messages/get_session_meta/list_user_sessions/get_session 加 agent_ids 读写 + 新增 _parse_json_list 辅助函数）
- `/workspace/app/models/session.py`（SessionMessage/SessionMeta 加 agent_ids 字段）
- `/workspace/app/services/chat_service.py`（持久化块取 orchestrator_service.last_agent_ids 并写入 new_messages）
