# 收藏与分享中文标题计划

## 背景与目标

会话有 `name`（首条用户消息截断 50 字符），但收藏（`message_favorites`）和分享（`message_shares`）没有名称。目标：**创建收藏/分享时生成中文标题（首条用户消息截断，与会话名称逻辑一致）并持久化**，详情接口展示 `title` 字段；存量旧数据在详情接口动态兜底。

用户已确认：标题 = 首条用户消息截断 50 字符；持久化到新列。

## 现状分析（探索结论）

- **会话名称参考逻辑**：[mysql_session_dao.py](file:///workspace/app/dao/mysql_session_dao.py#L735-L761) `_extract_name_from_state`、[session_dao.py](file:///workspace/app/dao/session_dao.py#L87-L93)——首条 role=user 消息文本 `[:50]`，无 LLM
- **分享**：[message_share_dao.py](file:///workspace/app/dao/message_share_dao.py) `create_share(session_id, user_id, pair_ids)` INSERT 4 列；`get_share` 返回 5 字段。路由 [message_share.py](file:///workspace/app/routes/message_share.py)：POST 做属主校验；GET 复用 `get_session_detail` 后按 pair_ids 过滤三列表
- **收藏**：[message_favorite_dao.py](file:///workspace/app/dao/message_favorite_dao.py) `create_favorite(user_id, pair_ids)` 单语句 INSERT...SELECT 从 messages 表复制（`WHERE m.user_id = %s AND m.message_pair_id IN (...) ORDER BY m.id ASC`，天然支持跨会话）；`list_favorites` 返回行 dict。路由 [message_favorite.py](file:///workspace/app/routes/message_favorite.py)：GET 按 favorite_id 分组返回 `{favorite_id, messages, files, upload_files}`；favorite 路由已从 share 路由导入 `_clean_pair_ids`（helper 共享先例）
- **DDL 兼容模式**：[init_mysql.py](file:///workspace/app/dao/init_mysql.py#L162-L175) `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（MySQL 8）
- **测试**：[test_message_share.py](file:///workspace/tests/test_message_share.py)、[test_message_favorite.py](file:///workspace/tests/test_message_favorite.py) 断言完整 SQL 字符串与 DAO 调用参数（`assert_awaited_once_with`），需同步更新

## 改动方案

### 1. [init_mysql.py](file:///workspace/app/dao/init_mysql.py) — DDL

- `CREATE TABLE message_shares`：`message_pair_ids` 之后加 `title VARCHAR(255) NOT NULL DEFAULT ''`
- `CREATE TABLE message_favorites`：`favorite_id` 之后加 `title VARCHAR(255) NOT NULL DEFAULT ''`
- ALTER 兼容区（L175 后）追加：
  ```sql
  ALTER TABLE message_shares ADD COLUMN IF NOT EXISTS (title VARCHAR(255) NOT NULL DEFAULT '');
  ALTER TABLE message_favorites ADD COLUMN IF NOT EXISTS (title VARCHAR(255) NOT NULL DEFAULT '');
  ```

### 2. [message_share_dao.py](file:///workspace/app/dao/message_share_dao.py)

- `create_share` 加参数 `title: str = ""`：INSERT 列与 VALUES 加 title
- `get_share`：SELECT 加 title（行 dict 自然带出）
- 新增 `get_first_user_message(user_id, session_id, message_pair_ids) -> str`：
  ```sql
  SELECT content FROM messages
  WHERE user_id = %s AND session_id = %s AND message_pair_id IN (...)
    AND role = 'user'
  ORDER BY id ASC LIMIT 1
  ```
  无行/异常返回 `""`（标题失败不阻断创建流程）

### 3. [message_favorite_dao.py](file:///workspace/app/dao/message_favorite_dao.py)

- `create_favorite` 加参数 `title: str = ""`：INSERT 列加 title，SELECT 首位加 `%s` 常量（同组各行共享同一 title，冗余存储符合本表"消息副本"风格）；`args = [favorite_id, title, user_id] + pair_ids`
- `list_favorites`：SELECT 加 title，行 dict 加 `"title": r.get("title") or ""`
- 新增 `get_first_user_message(user_id, message_pair_ids) -> str`：同 share 版但**不带 session_id**（与 INSERT...SELECT 的 WHERE 条件对齐，支持跨会话收藏）；`ORDER BY id ASC LIMIT 1`；无行/异常返回 `""`

### 4. [message_share.py](file:///workspace/app/routes/message_share.py) — 路由 + 共享 helper

新增两个模块级 helper（favorite 路由复用）：

```python
def _truncate_title(text: str) -> str:
    """标题 = 首条用户消息截断 50 字符（与会话名称逻辑一致）。"""
    return (text or "")[:50]

def _first_user_message_title(messages: List[dict]) -> str:
    """从消息 dict 列表取首条 user 消息文本截断；无则空串（旧数据兜底）。"""
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            return _truncate_title(str(m["content"]))
    return ""
```

- **POST /message_share**：属主校验通过后、`create_share` 之前：
  ```python
  raw = await share_dao.get_first_user_message(user.get("user_id"), session_id, pair_ids)
  title = _truncate_title(raw)
  shared_id = await share_dao.create_share(session_id, user.get("user_id"), pair_ids, title)
  ```
- **GET /message_share/{shared_id}**：过滤三列表之后加：
  ```python
  # 分享标题：创建时持久化；存量旧数据（title 为空）从被分享消息动态兜底
  data["title"] = share.get("title") or _first_user_message_title(data["messages"])
  ```

### 5. [message_favorite.py](file:///workspace/app/routes/message_favorite.py) — 路由

- 导入加 `_first_user_message_title, _truncate_title`（复用现有 import 行）
- **POST /message_favorite**：
  ```python
  raw = await favorite_dao.get_first_user_message(user.get("user_id"), pair_ids)
  title = _truncate_title(raw)
  favorite_id, copied = await favorite_dao.create_favorite(user.get("user_id"), pair_ids, title)
  ```
- **GET /message_favorites**：分组循环内（`favorites.append` 前）：
  ```python
  # 组内各行共享同一 title（创建时写入）；空则从组内消息兜底（存量旧数据）
  title = (messages[0].get("title") if messages else "") or _first_user_message_title(messages)
  ```
  `favorites.append({... "title": title ...})`（放在 favorite_id 旁）

## 假设与决策

1. **标题截断 50 字符、不 strip**：与会话名称逐字对齐（`raw_text[:50]`），最小惊讶
2. **title 冗余存储在收藏表每行**：同一 favorite_id 组内各行同值，读组内首行即可；避免再建收藏组表
3. **get_first_user_message 查询失败静默返回 ""**：分享/收藏创建不因标题生成失败而中断；详情接口的动态兜底保证展示
4. **DAO 层不截断**：DAO 返回完整 content，截断统一在 route helper（单一职责，helper 可测）
5. **创建接口响应不新增 title 字段**：需求只要求详情接口展示，保持最小变更

## 实施顺序

1. init_mysql.py DDL → 2. message_share_dao.py → 3. message_favorite_dao.py → 4. message_share.py 路由 + helper → 5. message_favorite.py 路由 → 6. 测试更新 → 7. 回归

## 验证步骤

```bash
python -m py_compile app/dao/init_mysql.py app/dao/message_share_dao.py app/dao/message_favorite_dao.py app/routes/message_share.py app/routes/message_favorite.py

# 定向：两个测试文件全过（含更新的 SQL 断言 + 新增标题用例）
python -m pytest tests/test_message_share.py tests/test_message_favorite.py -q

# 全量回归：基线 508 passed + 2 个预存 dashboard JWT 失败
python -m pytest tests/ -q
```

## 测试更新明细

**test_message_share.py**：
- `test_dao_create_share_sql_and_args`：SQL/args 断言加 title 列与值
- `test_dao_get_share_parses_json`：fake row 加 title 并断言透传
- 新增 DAO 测试：`get_first_user_message` 的 SQL 形态（含 role='user' / ORDER BY id ASC LIMIT 1）、IN 占位符、无行返回 ""
- `_make_share_dao`：get_share 返回值加 title、新增 get_first_user_message mock
- `test_post_share_success`：`create_share.assert_awaited_once_with("s1", "u1", ["p1", "p2"], <title>)`（mock 返回值截断后）
- GET 详情：断言 `data["title"]`；新增空 title 旧数据兜底用例（title=""，从被分享首条 user 消息现算）

**test_message_favorite.py**：
- `test_dao_create_favorite_insert_select`：SQL 断言加 title（`SELECT %s, %s, m.session_id` 形态）、args 加 title
- `test_dao_list_favorites_formats_rows`：行断言加 title
- 新增 DAO 测试：`get_first_user_message`（无 session_id 条件）
- route mock：create_favorite 断言加 title 参数；`_make` 工具的 rows 加 title
- GET 收藏列表：断言每组 title；新增空 title 兜底用例

## 风险与回滚

- **旧表无 title 列**：ALTER IF NOT EXISTS 兼容（项目已有同模式 13 条）；MySQL 5.7 会报 1064 被现有 try/except 吞掉（L204-213 已处理）
- **存量数据 title 为空**：详情接口动态兜底覆盖，不迁移
- **回滚**：git checkout 还原即可；新列 DEFAULT '' 不影响旧代码读写
