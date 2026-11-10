# 计划：收藏列表与收藏详情拆分为两个接口

## Summary
将 `GET /message_favorites/list`（当前返回全部收藏的完整消息+文件）拆分为：
1. **列表接口** `GET /message_favorites/list` — 改为轻量摘要：每个收藏返回 favorite_id / title / message_count / first_message_time，不返回消息内容与文件
2. **详情接口** `GET /message_favorites/{favorite_id}` — 按收藏 ID 返回该条完整详情（messages / files / upload_files），结构与原列表接口单个分组一致

## Current State Analysis
- [app/routes/message_favorite.py](app/routes/message_favorite.py#L103-L162)：`list_message_favorites` 一次拉全部收藏行（`list_favorites` DAO），按 favorite_id 分组，逐会话加载产出/上传文件，返回完整内容
- [app/dao/message_favorite_dao.py](app/dao/message_favorite_dao.py#L115-L155)：`list_favorites(user_id)` 返回全部行（含格式化）；行格式化逻辑内联在其中
- 响应格式（message_share.py L17-22）：`success_response` → `{"code": 200, "msg": "success", "data": {...}}`；`error_response(code, msg)` → `{"code": code, "msg": msg, "data": {}}`
- 路由已注册（main.py L210），新路由无需改 main.py
- 路由匹配顺序：`/message_favorites/list` 必须注册在 `/message_favorites/{favorite_id}` 之前，否则 "list" 会被当作 favorite_id

## Proposed Changes

### 1. app/dao/message_favorite_dao.py
a. **提取 `_format_rows(rows)` 静态方法**：把 `list_favorites` 内联的行格式化（timestamp strftime、JSON 列解析、bool/int 归一化）抽出，`list_favorites` 与新方法共用（`list_favorites` 保持原行为，现有测试不破坏）

b. **新增 `list_favorite_summaries(user_id)`** — 列表接口专用，一条 GROUP BY SQL：
```sql
SELECT favorite_id, MAX(title) AS title, COUNT(*) AS message_count,
       MIN(`timestamp`) AS first_message_time
FROM message_favorites
WHERE user_id = %s
GROUP BY favorite_id
ORDER BY MIN(id) ASC   -- id 序 = 收藏先后顺序
```
返回 `[{favorite_id, title, message_count, first_message_time}]`，first_message_time 用与消息相同的 strftime 格式

c. **新增 `get_favorite_messages(user_id, favorite_id)`** — 详情接口专用：与 `list_favorites` 相同 SELECT + `AND favorite_id = %s`，`ORDER BY id ASC`；空结果返回 `[]`（复用 `_format_rows`）

d. **新增 `get_favorite_first_user_message(user_id, favorite_id)`** — 列表接口存量旧数据（title 为空）的标题兜底：
```sql
SELECT content FROM message_favorites
WHERE user_id = %s AND favorite_id = %s AND role = 'user'
ORDER BY id ASC LIMIT 1
```
异常时返回空串（对齐 `get_first_user_message` 风格）

### 2. app/routes/message_favorite.py
a. **改造 `GET /message_favorites/list`**：改用 `list_favorite_summaries`；title 为空的（存量旧数据）逐条调 `get_favorite_first_user_message` + `_truncate_title` 兜底（存量少、循环可接受）；返回 `{"favorites": [摘要...]}`。原分组/文件加载逻辑从该路由移除

b. **新增 `GET /message_favorites/{favorite_id}`**（注册在 /list 之后）：
- `get_favorite_messages` 为空 → `error_response(404, "收藏不存在")`
- 复用现有 `_collect_session_pair_keys` + `_load_session_files_pair` 加载 files / upload_files（单收藏无分组，去掉缓存层，直接逐 session 加载）
- title：`rows[0].title` 或 `_first_user_message_title(rows)` 兜底（与原逻辑一致）
- 返回单个收藏完整结构：`{"favorite_id", "title", "messages", "files", "upload_files"}`

c. **更新模块 docstring**（接口清单变更）

## Assumptions & Decisions
- 详情路径 `GET /message_favorites/{favorite_id}`：对齐现有 `DELETE /message_favorite/{favorite_id}` 的路径风格
- 列表只给展示所需摘要（不含消息内容），明细按需走详情接口——这正是拆分的目的
- `first_message_time` 取组内 MIN(timestamp)（表无收藏时间列，timestamp 为消息原时间）
- `list_favorites` 保留（有测试覆盖，且为 get_favorite_messages 的格式化基础）
- 不改 main.py / models / init_mysql

## Verification（按用户要求最小化）
1. `python -m py_compile app/dao/message_favorite_dao.py app/routes/message_favorite.py`
2. 不跑 pytest（用户明确要求少做/不做测试）
3. 完成后在回复中展示两个接口的返回格式示例
