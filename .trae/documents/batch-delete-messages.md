# 计划：删除消息接口支持批量（请求体传 message_pair_ids 数组）

## Summary
将 `DELETE /sessions/{session_id}/messages/{message_pair_id}`（单条路径参数）改为 `DELETE /sessions/{session_id}/messages`，请求体 `{"message_pair_ids": [...]}` 一次删除多轮消息；删空会话仍整删会话。

## Current State Analysis
- 路由 [app/routes/sessions.py:94-114](app/routes/sessions.py#L94-L114)：单 pair_id 路径参数 → `service.delete_message(user_id, session_id, pair_id)`；403/404/结果处理已就绪
- 服务 [app/services/session_service.py:97-123](app/services/session_service.py#L97-L123)：属主校验 → `dao.delete_messages_by_pair` → 删除 0 行返回、删空 `count_messages==0` 整删会话
- DAO [app/dao/mysql_session_dao.py:565-578](app/dao/mysql_session_dao.py#L565-L578)：`DELETE FROM messages WHERE session_id=%s AND message_pair_id=%s`，返回 rowcount
- 清洗工具：`_clean_pair_ids`（[message_share.py:25-38](app/routes/message_share.py#L25-L38)，strip/去空/去重保序）
- sessions.py 请求体均用 `await request.json()` 模式（L53/L69）
- `delete_messages_by_pair` 与 `delete_message` 仅被该路由链路调用，无其他调用方

## Proposed Changes

### 1. app/dao/mysql_session_dao.py — `delete_messages_by_pair` 改批量
签名 `(self, session_id: str, message_pair_ids: List[str]) -> int`：
- 空列表直接 return 0（不发 SQL）
- `DELETE FROM messages WHERE session_id = %s AND message_pair_id IN (%s, ...)`，占位符按列表长度生成
- rowcount + commit，返回删除行数

### 2. app/services/session_service.py — `delete_message` 改批量
签名 `(self, user_id, session_id, message_pair_ids: List[str]) -> Optional[dict]`，流程不变：
1. `get_session_meta` → None 返回 None；属主不符 raise PermissionError
2. `deleted = dao.delete_messages_by_pair(session_id, message_pair_ids)`（一次 SQL 批量删）
3. `deleted == 0` → `{"deleted": 0, "session_deleted": False}`
4. `count_messages(session_id) == 0` → 整删会话 → `{"deleted": deleted, "session_deleted": True}`
5. 否则 `{"deleted": deleted, "session_deleted": False}`
docstring 同步更新（一轮→多轮）。返回值 `deleted` 为删除的消息**行数**（一轮 user+assistant 通常 2 行）。

### 3. app/routes/sessions.py — 替换路由
删除旧路由 `DELETE /sessions/{session_id}/messages/{message_pair_id}`，新增：
```python
@router.delete("/sessions/{session_id}/messages")
async def delete_session_messages(session_id: str, request: Request, user=Depends(current_user)):
    """批量删除会话中多轮消息；若删空则整删会话。"""
    # body: {"message_pair_ids": ["...", ...]}
    # _clean_pair_ids 清洗（from app.routes.message_share import）→ 空 400 "message_pair_ids 不能为空"
    # service.delete_message(user_id, session_id, pair_ids)
    # PermissionError → 403；None → 404 "会话不存在"；deleted==0 → 404 "消息不存在"
    # 成功 success_response(result)  # {"deleted": n, "session_deleted": bool}
```

## Assumptions & Decisions
- 路径去掉 `/{message_pair_id}` 段：数组语义下路径参数无法表达多条，请求体承载（用户明确要求）
- 旧单条路由直接删除（用户说"改成"，且前端尚未上线批量删除则同步更新；保留旧路由属于多余兼容）
- 部分不存在的 pair_id 不报错：SQL 只删匹配行，`deleted` 反映实际行数（与单条版行为一致）
- 一次 SQL 批量删（不逐条循环），事务原子性由单语句保证
- 不改 models / main.py

## Verification（按用户要求最小化）
1. `python -m py_compile` 3 个改动文件
2. 不跑 pytest
