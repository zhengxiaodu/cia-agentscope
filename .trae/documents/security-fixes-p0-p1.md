# 安全问题修复计划（P0-2 / P1-1~P1-4）

## Summary

按安全评审结论修复 5 个已核实问题（P0-1 密钥问题按用户要求跳过，不在本计划内）：

1. **P0-2** session_id 路径穿越 + 沙箱 shell 注入
2. **P1-2** 上传接口可向他人会话注入文件
3. **P1-1** 登录用户可下载他人持久化文件
4. **P1-3** 已删除会话 ID 被当新会话复用
5. **P1-4** 输出敏感检测命中后，历史持久化仍存原文

## Current State Analysis（已核实的证据）

| 问题 | 位置 | 现状 |
|---|---|---|
| session_id 无格式校验 | `app/services/session_service.py:38-47` | 不存在/已删除的 ID 原样放行；`chat.py:104`、`upload.py:88` 均经此入口 |
| 归档路径拼接 | `app/services/chat_service.py:444-461` | `_persist_file_bytes` 用 `os.path.join(persist_dir, session_id, rel_path)`，`../` 可逃逸归档根目录 |
| shell 未引用 | `app/services/opensandbox_workspace_manager.py:384,453` | `f"mkdir -p {session_dir}"` 未 `shlex.quote`（仓库 1005/1063 行已有 quote 先例） |
| 上传跳过属主校验 | `app/routes/upload.py:85-88` | 仅 session_id 缺失时调 `get_or_create_session`，传入他人会话 ID 直接落 `upload_files` |
| persist-files 无属主校验 | `app/routes/files.py:105-157` | 只验登录；持久化目录全用户共享 |
| 已删除=不存在 | `app/dao/mysql_session_dao.py:466-480` | `get_session_meta` 过滤 `deleted_at IS NULL`，删除与不存在同返 None；且 `append_messages:238-244` 对软删会话静默跳过落库（当前删后继续聊=白跑编排不保存） |
| 敏感原文落库 | `app/services/chat_service.py:1213-1219` | 流结束后检测、发 `message_replace`，但 ⑥ 持久化（`persist_history` 闭包于 892 行引用 `final_output`/`collected_content_parts`）仍用原文 |

既有可复用约定：`historical_file_service.py:31-35` 的属主校验模式（存在 + 属主 + 未删除，否则 PermissionError）。

## Proposed Changes

### 1. P0-2：session_id 入口校验 + 归档路径防御 + shell 引用

**`app/services/session_service.py`**（`get_or_create_session` 顶部）：

```python
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # 模块级

async def get_or_create_session(...):
    if session_id and not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="session_id 格式非法")
    ...
```

- 覆盖 chat 与 upload 两个入口（唯一创建链路），uuid4().hex 天然匹配
- 宽松字符集兼容历史 ID，同时阻断 `/ \ .. ; $ '` 等穿越与注入元字符

**`app/services/chat_service.py` `_persist_file_bytes`**（防御纵深，不依赖入口校验）：

```python
root = os.path.realpath(os.path.abspath(persist_dir))
full_path = os.path.join(persist_dir, session_id, rel_path)
if os.path.realpath(full_path) != root and not os.path.realpath(full_path).startswith(root + os.sep):
    logger.warning("[chat_service] 归档路径越界拒绝 session=%s rel=%s", session_id, rel_path)
    return False
```

**`app/services/opensandbox_workspace_manager.py:384,453`**：两处 `mkdir -p {session_dir}` 改为 `mkdir -p {shlex.quote(session_dir)} && echo 1`（文件顶部已有/补 `import shlex`）。

### 2. P1-2：上传无条件走属主校验

**`app/routes/upload.py:85-88`**，`if not session_id:` 分支改为无条件：

```python
if not session_id:
    session_service = request.app.state.session_service
session_id = await session_service.get_or_create_session(session_id, user_id)
```

- 他人已有会话 → 403（`get_or_create_session` 现有逻辑）；不存在 → 惰性放行（与现状一致）

### 3. P1-3：入口拒绝已删除会话

**`app/dao/mysql_session_dao.py`** 新增方法（与 `get_session_meta` 并列，复用其连接模式）：

```python
async def get_session_owner_status(self, session_id: str) -> Optional[dict]:
    """查询会话属主与删除状态（不过滤软删），供入口区分『已删除』与『不存在』。"""
    # SELECT user_id, deleted_at FROM sessions WHERE session_id = %s
```

**`app/services/session_service.py` `get_or_create_session`**：在 meta 为 None 的兜底返回前：

```python
owner_status = await self.dao.get_session_owner_status(session_id)
if owner_status is not None and owner_status.get("deleted_at") is not None:
    raise HTTPException(status_code=403, detail="会话已删除")
return session_id
```

- 修正现状：删后继续聊从"白跑编排不落库"变为显式 403，与 `append_messages` 的软删感知一致

### 4. P1-1：persist-files 下载加属主校验

**`app/services/session_service.py`** 新增轻量方法（不改 `get_session_meta` 签名，避免波及 SessionMeta 模型）：

```python
async def session_files_accessible(self, session_id: str, user_id: str) -> bool:
    """持久化文件下载授权：会话存在、属主匹配且未软删。"""
    owner_status = await self.dao.get_session_owner_status(session_id)
    return bool(
        owner_status
        and owner_status.get("deleted_at") is None
        and str(owner_status.get("user_id") or "") == str(user_id)
    )
```

**`app/routes/files.py` persist-files 路由**（403 越权校验后）：

```python
session_service = getattr(request.app.state, "session_service", None)
if session_service is None or not await session_service.session_files_accessible(
    session_id, user.get("user_id", "")
):
    raise HTTPException(status_code=403, detail="无权访问该路径")
```

### 5. P1-4：敏感命中时持久化脱敏文案

**`app/services/chat_service.py:1213-1219`** blocked 分支内，yield 前补两行（闭包按引用捕获，⑥ 调用时生效）：

```python
if sens_result["blocked"]:
    output_blocked = True
    # 持久化与 trace 用脱敏文案：避免命中内容经历史回显二次泄露
    final_output = SENSITIVE_BLOCK_MESSAGE
    collected_content_parts = [{"type": "text", "content": SENSITIVE_BLOCK_MESSAGE}]
    ...
```

- `citations`/`bocha_sum` 为来源信息保留不动；文件归档（archive_files）不受影响
- SSE 流内原文已发出的架构取舍不在本次范围（前置检测会杀死逐字流式体验）

## Assumptions & Decisions

1. **历史 session_id 兼容**：校验字符集取宽松的 `[A-Za-z0-9_-]{1,64}` 而非严格 32 位 hex，避免锁死存量 ID；实施时可用 SQL `SELECT session_id FROM sessions WHERE session_id NOT REGEXP '^[A-Za-z0-9_-]{1,64}$' LIMIT 10` 抽查确认（有命中再收紧讨论）。
2. **分享视图的取舍**：`message_share` 只按 pair 过滤返回文件元数据（含 url 字段），分享接收者打开 persist-files 链接将 403。评审建议的"按分享记录单独授权"作为后续独立事项，本次不做。
3. **删除会话 403 优于现状**：当前删后继续聊会静默丢历史（append_messages 跳过软删会话），403 是行为修正而非破坏。
4. **不动的内容**：.env/git 历史（用户自行处理）、SSE 流式前置检测、分享记录级授权、Redis 版 session_dao（主链路未使用）。

## Tests

1. **新增 `tests/test_session_id_validation.py`**：
   - `get_or_create_session`：`../x`、含空格/引号、超 64 位 → 400；uuid hex → 放行；mock dao 已删除 → 403
   - `_persist_file_bytes`：`session_id="../outside"` → 返回 False 且归档根外无文件
2. **扩展 `tests/test_persist_files_download.py`**：mock session_service，属主 → 200；他人/已删除/不存在 → 403（沿用现有 mock 基建）
3. **扩展 `tests/test_error_fallback.py`**：mock `check_sensitive` 命中 output，断言落库 content 为 `SENSITIVE_BLOCK_MESSAGE`（沿用现有 24 个用例的编排 mock 模式）
4. **上传属主校验用例**：mock `session_service.get_or_create_session` 抛 403，断言上传接口透传 403（并入新文件或 `test_uploads_list_delete.py`）

## Verification

1. 运行：新增测试 + `test_error_fallback` + `test_persist_files_download` + `test_uploads_list_delete` + `test_upload_inject_and_bind` + `test_session_files_persist` + `test_message_pair_and_citations` + `test_workspace_file_access` + `test_session_detail_upload_files`
2. 全量 `pytest tests` 与已知存量失败清单（29 个：意图过滤 9 / 收藏 7 / 敏感 mock 3 / 沙箱往返 3 / upload_persist 2 / policy 2 / bocha 2 / redis 1）对比，**不新增任何失败**即通过
3. 两个后端实例重启后手动冒烟：正常上传+提问（首轮建会话、已有会话续聊）、删除会话后再次提问应 403、本人下载自己会话文件正常
