# 收藏详情补文件列表 + 外部意图 status 过滤 改造计划

## Summary

两个改造点：

1. **收藏详情补文件展示**：`GET /message_favorites` 每个收藏分组下新增 `files`（系统产出文件，来自 session_files 表）和 `upload_files`（用户上传文件，来自 upload_files 表）两个字段，字段格式与历史会话详情接口（get_session_detail）完全对齐
2. **外部意图禁用过滤**：`/api/intents` 返回的每个意图新增 `status` 字段（0=禁用，1=启用），在 `fetch_external_intents` 获取后统一过滤掉禁用意图，所有下游（登录 description 注入、agent_access、意图合并进 Redis）自动生效

## Current State Analysis

- **收藏路由**（[message_favorite.py](file:///workspace/app/routes/message_favorite.py#L78-L98)）：`GET /message_favorites` 目前仅调 `favorite_dao.list_favorites` 按 favorite_id 分组返回 `{favorite_id, messages}`，无任何文件信息
- **收藏消息已带定位键**：list_favorites 每条消息含 `session_id` + `message_pair_id`，足以定位关联文件
- **文件 DAO 已就绪且可直接复用**：
  - [mysql_session_dao.py load_session_files](file:///workspace/app/dao/mysql_session_dao.py#L612-L640)：按 session_id 查 session_files，返回 name/path/url/size/media_type/created_at/message_id/message_pair_id
  - [upload_file_dao.py list_files_by_session](file:///workspace/app/dao/upload_file_dao.py#L213-L244)：按 session_id 查 upload_files，返回 name/size/media_type/created_at/message_id/message_pair_id
  - 两个 DAO 均已挂载 app.state（session_dao / upload_file_dao，[main.py](file:///workspace/app/main.py#L119-L121)），路由层可直接取用
- **跨会话收藏**：一个收藏分组内的多个 pair 可能来自不同会话（收藏接口不限定 session），文件匹配必须按 `(session_id, message_pair_id)` 二元组，不能只按 pair_id
- **意图获取唯一入口**：[mng_service.py fetch_external_intents](file:///workspace/app/services/mng_service.py#L18-L79) 是 /api/intents 的唯一调用方，auth.py 登录与 orchestrator_service.build_and_cache_user_config 都经它取数，在函数内部过滤即可全链路生效
- **现有测试**：test_agent_access_description.py 仅测 build_agent_definition_map（不受影响）；test_message_favorite.py 的 GET 路由测试需同步补 mock

## Proposed Changes

### 1. 收藏详情补文件（[message_favorite.py](file:///workspace/app/routes/message_favorite.py)）

`GET /message_favorites` 路由改造（保持 DAO 不动，文件查询在路由层聚合）：

```python
@router.get("/message_favorites")
async def list_message_favorites(request, user=Depends(current_user)):
    favorite_dao = ...                      # 现有
    session_dao = request.app.state.session_dao          # 新取
    upload_file_dao = request.app.state.upload_file_dao  # 新取（可能为 None，容错）

    rows = await favorite_dao.list_favorites(user_id)
    # 分组（现有逻辑不变）
    groups: Dict[str, List[dict]] = {}
    for row in rows:
        groups.setdefault(row["favorite_id"], []).append(row)

    # ---- 新增：按 (session_id, message_pair_id) 收集分组关联键 ----
    # 全局收集唯一 session_id，每个 session 只查一次库（避免多分组重复查询）
    session_pair_keys = {                    # {session_id: set(pair_id)}
        sid: {m["message_pair_id"] for m in msgs if m.get("message_pair_id")}
        ...
    }

    # 对每个唯一 session 查一次文件，缓存 {session_id: {"files": [...], "uploads": [...]}}
    # - files: session_dao.load_session_files(session_id)
    # - uploads: upload_file_dao.list_files_by_session(session_id)
    #   upload_file_dao 为 None 或查询异常 → 该 session 的 uploads 为 []（对齐
    #   get_session_detail 中的容错风格），session_dao 异常则向上抛（与详情接口
    #   的 files 无容错一致）

    # 每个分组：files/upload_files = 该组涉及 session 的文件中
    #   message_pair_id ∈ 该组该 session 的 pair 集合 的条目
    #   （未绑定 message_pair_id 的文件 None ∉ 集合，自然排除）

    favorites = [
        {
            "favorite_id": fid,
            "messages": messages,
            "files": [...],          # 新增：系统产出文件
            "upload_files": [...],   # 新增：用户上传文件
        }
        for fid, messages in groups.items()
    ]
```

返回结构（对齐历史会话详情接口的文件字段）：

```json
{
  "favorites": [
    {
      "favorite_id": "a1b2c3d4e5f67890",
      "messages": [ ... ],
      "files": [
        {"name": "a.md", "path": "a.md", "url": "/files/xxx/a.md",
         "size": 1024, "media_type": "text/markdown",
         "created_at": "2026-09-15 10:00:00.123",
         "message_id": 101, "message_pair_id": "p1"}
      ],
      "upload_files": [
        {"name": "up.pdf", "size": 2048, "media_type": "application/pdf",
         "created_at": "2026-09-15 09:59:00.000",
         "message_id": 100, "message_pair_id": "p1"}
      ]
    }
  ]
}
```

### 2. 外部意图 status 过滤（[mng_service.py](file:///workspace/app/services/mng_service.py#L72-L76)）

`fetch_external_intents` 中 `data = body.get("data", [])` 校验为 list 后、return 前追加：

```python
# 过滤禁用意图：status=0 禁用；字段缺失/None 视为启用（兼容旧版 mng 返回）
enabled = [
    x for x in data
    if not (isinstance(x, dict) and x.get("status") == 0)
]
return enabled
```

- 过滤在唯一入口处做一次，下游全部自动生效：
  - 登录/注册/refresh/me 的 `agent_access` description 注入（build_agent_definition_map 输入即已过滤）
  - orchestrator 合并进 Redis 的 merged_intents/merged_agents/merged_skills（禁用意图及其 agent/skill 不再进入用户配置）
- status 为非 0 非 1 的异常值（如 2）按"非 0"处理保留，仅明确过滤 0

### 3. 测试

- **[test_message_favorite.py](file:///workspace/tests/test_message_favorite.py) 更新**：
  - `_make_app` 补挂 `session_dao` / `upload_file_dao`（MagicMock + AsyncMock）
  - `test_get_favorites_grouped`：mock 两个 DAO 返回含 message_pair_id 的文件，断言分组下 files/upload_files 只含关联 pair 的文件（跨 pair 的被剔除、message_pair_id=None 的被排除）
  - 新增跨会话分组用例：组内两个 pair 来自不同 session，各自只匹配本 session 的文件
  - `test_get_favorites_empty`：无收藏时不触发文件查询
- **新建 tests/test_intent_status_filter.py**：
  - `fetch_external_intents` 过滤逻辑：mock httpx（对齐现有 fake 模式或直接 monkeypatch httpx.AsyncClient）构造含 status=0/1/缺失 的返回，断言仅 status=0 被剔除
  - 无 status 字段全部保留（旧版兼容）
  - 非列表 data 返回 []（现有行为不回归）

## Assumptions & Decisions

1. **status 缺失/None → 视为启用**：兼容旧版 mng（未加 status 字段时行为不变）；仅明确过滤 `status == 0`
2. **过滤位置在 fetch_external_intents 内部**：唯一入口一次过滤，登录链路与 Redis 配置链路自动一致，无需改任何调用方
3. **收藏文件查询在路由层聚合**（不动 favorite DAO）：复用 load_session_files / list_files_by_session，字段格式天然与历史会话详情接口对齐；不写新 SQL
4. **文件按 `(session_id, message_pair_id)` 匹配**：跨会话收藏时不会把 A 会话的文件错配到 B 会话的同名 pair；message_pair_id 为 None 的文件（未消费绑定）不属于任何收藏分组
5. **每个唯一 session 只查一次库**：多分组共享 session 时复用缓存结果，避免 N×M 查询
6. **容错对齐 get_session_detail**：upload_file_dao 为 None 或查询异常 → 该 session uploads 置 []；session_files 查询异常不吞（与详情接口对 files 的处理一致）
7. files/upload_files 内不再嵌套 session_id 字段（分组消息里已带，避免冗余）；若前端需要可从消息取

## Verification

1. `python -m py_compile app/routes/message_favorite.py app/services/mng_service.py`
2. `python -m pytest tests/test_message_favorite.py tests/test_intent_status_filter.py tests/test_agent_access_description.py -v` 全绿
3. `python -m pytest tests/ -q` 全量回归（已知 2 个 regulations dashboard 401 存量失败与本次无关）
4. 手动验证（可选）：`GET /message_favorites` 看分组下 files/upload_files；配置一个 status=0 的意图后登录，确认 agent_access 与意图识别均不再出现该意图
