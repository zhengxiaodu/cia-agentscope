# /sessions 会话列表接口分页改造计划

## Summary

将 `GET /sessions`（展示所有会话）从固定 15 条改为分页接口：前端通过 query 参数 `page` / `page_size` 指定分页信息，响应中新增 `pagination` 元数据（total / total_pages / has_more 等）。默认参数（不传）行为与现状完全一致（第 1 页、每页 15 条），向后兼容。

## Current State Analysis

现状链路（3 层，均硬编码 15）：

1. **路由层** [app/routes/sessions.py:21-33](file:///workspace/app/routes/sessions.py#L21-L33)
   - `list_sessions` 调用 `service.list_user_sessions(user_id, limit=15)`，无任何 query 参数
   - 响应：`{"top_sessions": [...], "sessions": [...]}`
2. **服务层** [app/services/session_service.py:91-94](file:///workspace/app/services/session_service.py#L91-L94)
   - `list_user_sessions(user_id, limit=15)` 透传给 DAO，结果转 `SessionMeta`
3. **DAO 层** [app/dao/mysql_session_dao.py:415-496](file:///workspace/app/dao/mysql_session_dao.py#L415-L496)
   - `list_user_sessions(user_id, limit=15, pinned_limit=5)`
   - 置顶会话：`is_pinned=1 ORDER BY pinned_at DESC LIMIT 5`
   - 非置顶会话：`is_pinned=0 ORDER BY updated_at DESC LIMIT fetch_limit`，其中 `fetch_limit = limit + len(top_sessions)`（历史遗留 padding；查询已过滤 `is_pinned=0`，循环内跳过 pinned_ids 属死逻辑）

关键事实：
- 生产环境只使用 MySQL `SessionDAO`（[main.py:115-119](file:///workspace/app/main.py#L115-L119)）；Redis 版 `app/dao/session_dao.py` 无任何 import 引用，为遗留代码，**本次不改**
- 现无针对该列表接口的测试

## Proposed Changes

### 1. DAO 层：[app/dao/mysql_session_dao.py](file:///workspace/app/dao/mysql_session_dao.py) `list_user_sessions`

签名改为：

```python
async def list_user_sessions(
    self,
    user_id: str,
    page: int = 1,
    page_size: int = 15,
    pinned_limit: int = 5,
) -> tuple[list[dict], list[dict], int]:
    """返回 (top_sessions, sessions, total)。

    total: 该用户非置顶会话总数（用于分页元数据）
    """
```

改动点（均在同一连接/游标块内）：
- 置顶会话查询**不变**（`LIMIT pinned_limit`）
- 非置顶会话查询改为真正的分页 SQL：
  ```sql
  SELECT ... FROM sessions
  WHERE user_id = %s AND is_pinned = 0
  ORDER BY updated_at DESC
  LIMIT %s OFFSET %s
  ```
  参数：`(user_id, page_size, (page - 1) * page_size)`
  - **删除** `fetch_limit = limit + len(top_sessions)` padding 和循环内 `pinned_ids` 跳过、`len(sessions) >= limit` 截断（查询已过滤 `is_pinned=0`，均为死逻辑；保留会破坏 OFFSET 语义）
- 新增总数查询（放在分页查询前，同一游标）：
  ```sql
  SELECT COUNT(*) AS cnt FROM sessions WHERE user_id = %s AND is_pinned = 0
  ```
- 返回三元组 `(top_sessions, sessions, total)`

行→dict 映射逻辑复用现状（不动）。

### 2. 服务层：[app/services/session_service.py](file:////workspace/app/services/session_service.py) `list_user_sessions`

签名改为：

```python
async def list_user_sessions(
    self, user_id: str, page: int = 1, page_size: int = 15
) -> tuple[list[SessionMeta], list[SessionMeta], int]:
    """返回 (top_sessions, sessions, total_non_pinned)。"""
    raw_top, raw_list, total = await self.dao.list_user_sessions(
        user_id, page=page, page_size=page_size
    )
    return (
        [SessionMeta(**m) for m in raw_top],
        [SessionMeta(**m) for m in raw_list],
        total,
    )
```

### 3. 路由层：[app/routes/sessions.py](file:///workspace/app/routes/sessions.py) `list_sessions`

新增 query 参数（FastAPI 自动校验，非法值返回 422）：

```python
from fastapi import Query

@router.get("/sessions")
async def list_sessions(
    request: Request,
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(15, ge=1, le=100, description="每页条数，默认 15，最大 100"),
    user: dict = Depends(current_user),
):
    service = _get_session_service(request)
    top_list, session_list, total = await service.list_user_sessions(
        user.get("user_id"), page=page, page_size=page_size
    )
    total_pages = (total + page_size - 1) // page_size
    return success_response({
        "top_sessions": [s.model_dump(mode="json") for s in top_list],
        "sessions": [s.model_dump(mode="json") for s in session_list],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_more": page < total_pages,
        },
    })
```

### 4. 新增测试：tests/test_sessions_pagination.py

风格对齐现有路由测试（TestClient + mock app.state）：

**DAO 层**（fake pool/cursor，对齐 test_message_pair_and_citations.py 的 `_FakeCursor` 模式）：
- 分页 SQL 正确性：非置顶查询含 `LIMIT %s OFFSET %s`，参数为 `(user_id, page_size, offset)`；置顶查询不变
- COUNT 查询被执行且返回 total
- 第 2 页 offset 计算：page=2, page_size=10 → offset=10
- 返回三元组结构

**路由层**（TestClient + mock session_service）：
- 不传参数 → 默认 page=1 / page_size=15（向后兼容基线）
- 传 `?page=2&page_size=10` → service 收到 page=2, page_size=10
- pagination 元数据正确性：total=42, page_size=15 → total_pages=3, page=1 has_more=True, page=3 has_more=False
- 越界页（page 超过总页数）→ sessions 为空列表，has_more=False，正常 200
- 非法参数：page=0 / page_size=0 / page_size=101 → 422

## Assumptions & Decisions

1. **分页只作用于非置顶会话（sessions）**；`top_sessions`（置顶，最多 5 条）每页都完整返回——对齐主流聊天产品"置顶常驻列表顶部"的交互，且与现有响应结构（两列表分离）一致。
2. **采用页码式分页**（page/page_size + LIMIT/OFFSET），非游标式——与"前端指定分页信息"的表述吻合，实现最简；会话列表量级下 OFFSET 性能足够。
3. **page_size 上限 100**，防止恶意大分页拖库。
4. **响应向后兼容**：新增 `pagination` 字段为增量字段；不传参数时行为与现状一致（15 条）。前端可忽略 pagination 直到适配完成。
5. **Redis 版 DAO（app/dao/session_dao.py）不动**：无引用的遗留代码，避免无效改动。
6. `SessionListResponse` 模型（models/session.py:49-50）现无路由使用（路由手工构造 dict），不动。

## Verification

1. `python -m py_compile app/dao/mysql_session_dao.py app/services/session_service.py app/routes/sessions.py`
2. `python -m pytest tests/test_sessions_pagination.py -v`（新增测试全绿）
3. `python -m pytest tests/ -q`（全量回归 414+ 用例无回归）
4. 手动验证（可选，需服务环境）：
   - `curl "localhost:8000/sessions"` → 默认第 1 页 15 条 + pagination
   - `curl "localhost:8000/sessions?page=2&page_size=10"` → 第 2 页
   - `curl "localhost:8000/sessions?page=0"` → 422
