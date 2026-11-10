# /login 与 /chat 异步上报 mng active

## 背景与目标

每次调用 `/login` 和 `/chat` 接口时，向 `{MNG_AUTH_URL}/api/me/active` 发送一个 POST 请求，请求头带上本系统签发的 JWT 登录 token。该调用需异步执行（fire-and-forget），不耽误主流程返回，失败也不影响主流程。

## 当前状态分析

### 接口位置
- `/login`：[auth.py:106-111](file:///workspace/app/routes/auth.py#L106-L111)，JWT token 在 [_build_auth_success](file:///workspace/app/routes/auth.py#L42-L103) 第 59 行 `token = create_access_token(token_payload)` 生成后即可用
- `/chat`：[chat.py:13-46](file:///workspace/app/routes/chat.py#L13-L46)，通过 `Depends(current_user)` 解析 JWT（返回解码 dict），原始 token 字符串需从 `request.headers.get("authorization")` 取

### MNG_AUTH_URL 配置
- [config.py:67](file:///workspace/app/config.py#L67)：`MNG_AUTH_URL = os.getenv("MNG_AUTH_URL", "")`，格式 `http://localhost:7009`（不带尾斜杠）

### 现有 mng HTTP 调用模式（统一用 httpx.AsyncClient）
集中在 [user_dao.py](file:///workspace/app/dao/user_dao.py)，模式：
```python
async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.post(url, json=..., headers={"Authorization": f"Bearer {jwt_token}"})
```
已有函数：`verify_login_via_mng` / `register_via_mng` / `update_name_via_mng` / `update_department_via_mng` / `update_password_via_mng`。

### fire-and-forget 现状
项目无现成 fire-and-forget 模式。唯一 `asyncio.create_task` 在 [workspace_manager.py:140-146](file:///workspace/app/services/workspace_manager.py#L140-L146) 用于常驻清扫循环。本任务用 `asyncio.create_task` 实现单次 fire-and-forget。

### JWT token 获取方式
- `/login`：token 在 `_build_auth_success` 内是局部变量（[auth.py:59](file:///workspace/app/routes/auth.py#L59)），直接可用
- `/chat`：原始 token 需从请求头取，复用 [_require_jwt_from_header](file:///workspace/app/routes/auth.py#L127-L135) 模式（但该函数会抛 401，不适合此处；改为直接从 `request.headers.get("authorization")` 解析，解析失败则不上报）

## 设计决策

1. **函数位置**：放 [user_dao.py](file:///workspace/app/dao/user_dao.py)，与现有 `*_via_mng` 函数一致（虽然命名是 DAO，但已承担 mng 外部 HTTP 调用职责）。
2. **函数签名**：`async def notify_mng_active(jwt_token: str) -> None`，内部 try/except 吞掉所有异常仅记日志。
3. **fire-and-forget 包装**：新增 `def fire_notify_mng_active(jwt_token: str) -> None`（同步函数），内部用 `asyncio.create_task(notify_mng_active(jwt_token))` 投递后台任务，并设置 `task.add_done_callback` 记录未捕获异常。若当前无事件循环（极少见）则静默跳过。
4. **HTTP 调用**：跟随现有模式 `httpx.AsyncClient(timeout=10.0)`，POST 到 `f"{MNG_AUTH_URL}/api/me/active"`，headers 带 `Authorization: Bearer {jwt_token}`，无请求体（或空 JSON `{}`）。MNG_AUTH_URL 为空时静默跳过。
5. **调用时机**：
   - `/login`：在 `_build_auth_success` 中 token 生成后（[auth.py:59](file:///workspace/app/routes/auth.py#L59) 之后）调用 `fire_notify_mng_active(token)`，在 Redis 写入之前，确保登录成功即上报。
   - `/chat`：在 `chat()` 路由函数开头（[chat.py:14](file:///workspace/app/routes/chat.py#L14) 之后），从 `request.headers.get("authorization")` 取原始 token，解析出 Bearer 后的 token 字符串，调用 `fire_notify_mng_active(token)`。
6. **失败处理**：`notify_mng_active` 内部所有异常（网络/超时/非 200/业务码错误）仅记 `logger.warning`，不抛出。fire 包装层用 `add_done_callback` 兜底未捕获异常。
7. **不上报的情况**：MNG_AUTH_URL 为空、token 为空、请求头格式异常——均静默跳过，不记错误日志（避免噪音）。

## 改动清单

### 1. 修改 `/workspace/app/dao/user_dao.py`

**新增 import**（顶部已有 `httpx`、`logging`、`asyncio` 需确认）：
- `asyncio`（用于 `create_task`）

**新增两个函数**（放在文件末尾或 `update_password_via_mng` 之后）：

```python
async def notify_mng_active(jwt_token: str) -> None:
    """向 mng 上报用户活跃（POST /api/me/active）。

    异步 fire-and-forget，所有异常仅记日志，不抛出。
    """
    if not MNG_AUTH_URL or not jwt_token:
        return
    url = f"{MNG_AUTH_URL}/api/me/active"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={},
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
            if resp.status_code != 200:
                logger.warning(
                    f"[mng_active] 上报失败 status={resp.status_code} "
                    f"body={resp.text[:200]}"
                )
                return
            body = resp.json()
            if body.get("code") != 200:
                logger.warning(
                    f"[mng_active] mng 业务码异常 code={body.get('code')} "
                    f"msg={body.get('message')}"
                )
    except Exception:
        logger.warning("[mng_active] 上报异常", exc_info=True)


def fire_notify_mng_active(jwt_token: str) -> None:
    """fire-and-forget 包装：投递后台任务上报 mng active，不阻断主流程。

    若当前无运行中的事件循环则静默跳过。
    """
    if not MNG_AUTH_URL or not jwt_token:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环，跳过
        return
    task = loop.create_task(notify_mng_active(jwt_token))
    # 兜底：记录任务中未捕获的异常，避免"Task exception was never retrieved"警告
    task.add_done_callback(
        lambda t: t.exception() if not t.cancelled() and t.exception() else None
    )
```

注意：
- 用 `asyncio.get_running_loop()` + `loop.create_task()` 而非 `asyncio.create_task()`，便于显式捕获 `RuntimeError`（无事件循环时跳过）。
- `add_done_callback` 中调用 `t.exception()` 消费异常，避免 asyncio 警告。
- `logger` 已在 user_dao.py 顶部定义（需确认；若无则新增 `logger = logging.getLogger(__name__)`）。

### 2. 修改 `/workspace/app/routes/auth.py`

**新增 import**：
```python
from app.dao.user_dao import (
    verify_login,
    register as register_user,
    update_name_via_mng,
    update_department_via_mng,
    update_password_via_mng,
    fire_notify_mng_active,  # 新增
)
```

**在 `_build_auth_success` 中调用**（[auth.py:59](file:///workspace/app/routes/auth.py#L59) `token = create_access_token(token_payload)` 之后、第 61 行 `refresh_token = create_refresh_token(...)` 之前或之后均可，建议放在 Redis 写入之前）：

```python
token = create_access_token(token_payload)
# 同时签发 refresh token（固定有效期 7 天，不滚动刷新）
refresh_token = create_refresh_token(token_payload)

# 异步上报 mng 用户活跃（fire-and-forget，失败不影响登录）
fire_notify_mng_active(token)
```

注意：`register` 路由也走 `_build_auth_success`（[auth.py:116](file:///workspace/app/routes/auth.py#L116)），所以注册成功也会上报，符合预期（注册即登录）。

### 3. 修改 `/workspace/app/routes/chat.py`

**新增 import**：
```python
from app.dao.user_dao import fire_notify_mng_active
```

**在 `chat()` 路由函数开头调用**（[chat.py:14](file:///workspace/app/routes/chat.py#L14) `async def chat(...)` 之后，`user_id = user.get("user_id")` 之前或之后均可）：

```python
# 异步上报 mng 用户活跃（fire-and-forget，失败不影响对话）
authorization = request.headers.get("authorization", "")
if authorization.lower().startswith("bearer "):
    jwt_token = authorization.split(" ", 1)[1].strip()
    if jwt_token:
        fire_notify_mng_active(jwt_token)
```

注意：
- 不复用 `_require_jwt_from_header`（那个会抛 401，不适合此处——`current_user` 依赖已校验过 JWT，这里只是取原始 token 字符串用于转发）。
- 解析失败（无 Authorization 头或格式异常）则不上报，不记错误日志（`current_user` 已保证 JWT 有效，此处仅是防御性取值）。

## 假设与边界

- `/api/me/active` 是 mng 系统已存在的接口，接受空 body + Authorization 头，返回 `{code, message, data}`（与 mng 其他接口一致）。若 mng 接口签名不同（如需要 body 或返回格式不同），需调整 `notify_mng_active` 内的请求/校验逻辑。
- `/register` 通过 `_build_auth_success` 也会触上报（注册即登录，符合预期）。
- `/refresh` 接口**不**上报（用户未明确要求；若需要可后续追加）。
- fire-and-forget 任务在主流程响应返回后仍可能运行，依赖 httpx 自身连接管理；进程异常退出时任务可能丢失（可接受）。
- 每次调用新建 `httpx.AsyncClient`（跟随现有模式，无连接池复用）；若高频调用需优化，可后续改为共享 client。

## 验证步骤

1. **静态校验**：`python -m py_compile` 对 3 个改动文件
   - `app/dao/user_dao.py`
   - `app/routes/auth.py`
   - `app/routes/chat.py`
2. **grep 核对**：
   - `notify_mng_active` / `fire_notify_mng_active` 在 user_dao.py 中定义
   - `fire_notify_mng_active` 在 auth.py 和 chat.py 中被调用
   - `/api/me/active` 出现在 user_dao.py
3. **git 提交并 push** 到 `origin/trae/agent-5CYjia`

## 改动文件清单

- `/workspace/app/dao/user_dao.py`（新增 `notify_mng_active` + `fire_notify_mng_active` 函数）
- `/workspace/app/routes/auth.py`（`_build_auth_success` 中调用 `fire_notify_mng_active(token)`）
- `/workspace/app/routes/chat.py`（`chat()` 开头从请求头取 token 调用 `fire_notify_mng_active`）
