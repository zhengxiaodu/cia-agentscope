# 计划：删除消息接口 + 收藏重命名接口

## Summary
1. 新增删除消息接口：`DELETE /sessions/{session_id}/messages/{message_pair_id}`，删除会话中该轮消息（user+assistant）；若删空后会话无剩余消息，整删会话（复用现有 CASCADE 逻辑）。
2. 新增收藏重命名接口：`PUT /message_favorite/name`，输入 favorite_id 与新名称，更新该组收藏的 title。

## Current State Analysis
- 消息存储在 MySQL `messages` 表（含 session_id、message_pair_id），`sessions` 删除时 CASCADE 自动清理 `messages`/`agent_states`/`session_files`（init_mysql.py L33/49/67 已确认 FK）。
- `MySQLSessionDAO`（app.state.session_dao）已有：`get_session_meta`（属主校验）、`delete_session`、`rename_session`（UPDATE rowcount>0 模式）。
- `SessionService.delete_session` 是现有整删会话入口；sessions.py 路由统一走 session_service。
- `MessageFavoriteDAO` 已有 `delete_favorite`；`message_favorites.title` 列（VARCHAR(255)）上一任务已加，组内各行冗余同值。
- 收藏路由已有 success_response/error_response 工具（从 message_share 导入）。

## Proposed Changes

### 1. app/dao/mysql_session_dao.py — 新增两个方法
```python
async def delete_messages_by_pair(self, session_id: str, message_pair_id: str) -> int:
    """删除会话中指定 message_pair_id 的消息（一轮 user+assistant），返回删除行数。"""
    # DELETE FROM messages WHERE session_id = %s AND message_pair_id = %s
    # cur.rowcount + commit，风格对齐 delete_session

async def count_messages(self, session_id: str) -> int:
    """统计会话剩余消息数（判断删空后是否整删会话）。"""
    # SELECT COUNT(*) AS cnt FROM messages WHERE session_id = %s
```

### 2. app/services/session_service.py — 新增 delete_message
```python
async def delete_message(self, user_id: str, session_id: str, message_pair_id: str) -> Optional[dict]:
    """删除会话中一轮消息；删空则整删会话。

    返回 None=会话不存在；{"deleted": n, "session_deleted": bool}。
    属主不符 raise PermissionError（对齐 get_session_detail）。
    """
    # 1. get_session_meta → None 返回 None；user_id 不符 raise PermissionError
    # 2. deleted = dao.delete_messages_by_pair(session_id, message_pair_id)
    # 3. deleted == 0 → {"deleted": 0, "session_deleted": False}
    # 4. dao.count_messages(session_id) == 0 → dao.delete_session(session_id, user_id)
    #    → {"deleted": deleted, "session_deleted": True}
    # 5. 否则 {"deleted": deleted, "session_deleted": False}
```

### 3. app/routes/sessions.py — 新增路由
```python
@router.delete("/sessions/{session_id}/messages/{message_pair_id}")
async def delete_session_message(session_id, message_pair_id, request, user=Depends(current_user)):
    # try: result = service.delete_message(...)
    # except PermissionError → 403 "会话不属于当前用户"
    # result is None → 404 "会话不存在"
    # result["deleted"] == 0 → 404 "消息不存在"
    # return success_response(result)  # {"deleted": n, "session_deleted": bool}
```

### 4. app/dao/message_favorite_dao.py — 新增 rename_favorite
```python
async def rename_favorite(self, user_id: str, favorite_id: str, title: str) -> int:
    """重命名收藏：更新该 favorite_id 全部行的 title，返回更新行数。"""
    # UPDATE message_favorites SET title = %s WHERE user_id = %s AND favorite_id = %s
```

### 5. app/routes/message_favorite.py — 新增路由
```python
@router.put("/message_favorite/name")
async def rename_message_favorite(request, user=Depends(current_user)):
    # body: {"favorite_id": ..., "name": ...}
    # 校验：favorite_id 非空(400)、name 非空(400)、len(name) <= 255(400)——对齐 rename_session
    # updated = favorite_dao.rename_favorite(user_id, favorite_id, name)
    # updated == 0 → 404 "收藏不存在"
    # return success_response({"favorite_id": ..., "name": name})
```

## Assumptions & Decisions
- 删除消息需带 session_id（路径参数）：属主校验依赖会话，前端在会话详情页内操作、天然有 session_id；message_pair_id 按会话作用域删除。
- 删空判定用「删除后剩余消息数为 0」，整删复用 `delete_session`（CASCADE 清理关联表）。
- 重命名用 PUT（对齐 `PUT /sessions/{session_id}/name`），路径按用户指定 `/message_favorite/name`。
- MySQL UPDATE 同值 rowcount=0 的边界（重命名为原名返回 404）：与现有 `rename_session` 行为一致，不特殊处理。
- 不改 main.py / routes/__init__.py（两个 router 已注册）。
- 按用户要求：不做或只做最少测试。

## Verification（最小验证）
1. `python -m py_compile` 5 个改动文件。
2. 快速跑现有相关测试确认无 import 破坏（可选，~4s）：
   `python -m pytest tests/test_message_favorite.py tests/test_message_share.py -q`
