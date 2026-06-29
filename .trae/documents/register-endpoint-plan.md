# 注册接口实现计划

## 摘要

在后端新增 `POST /register` 接口：接收 `{username, password}`，向 mng 管理中心 `POST {MNG_URL}/api/auth/register` 发起注册请求；注册成功后，复用登录流程（保存 mng 的 `access_token` + `permissions` 到 Redis、基于 `user_info` 生成 JWT），最终返回与 `/login` 接口**完全相同**的字段结构。

## 现状分析

经探索确认，现有登录链路如下（均位于 `/workspace/app`）：

| 关注点 | 文件 | 现状 |
|---|---|---|
| 登录路由 | `routes/auth.py` | `@router.post("/login")`，调用 `verify_login` → 存 Redis → 生成 JWT → 返回 `success_response` |
| mng 调用 | `dao/user_dao.py` | `verify_login_via_mng` 用 `httpx.AsyncClient` POST `{MNG_URL}/api/auth/login`，把 mng 返回标准化为 `{verification, user_info, access_token, permissions}` |
| mock 模式 | `dao/user_dao.py` | `AUTH_MOCK=true` 时走 `_MOCK_USERS`，否则走 mng |
| JWT | `services/auth_service.py` | `create_access_token(payload)` 用 PyJWT/HS256，TTL=`JWT_EXPIRE_HOURS` |
| Redis 存储 | `services/auth_service.py` | `save_user_permissions(redis_client, user_id, access_token, permissions)`，key=`user_permissions:{user_id}`，TTL 同 JWT |
| 请求模型 | `models/auth.py` | `LoginRequest{username,password}`、`UserInfo`、`LoginResponse` |
| 路由装配 | `main.py` | `app.include_router(auth.router, tags=["auth"])`，无前缀 |
| 配置 | `config.py` + `.env` | `MNG_URL`、`JWT_SECRET`、`JWT_EXPIRE_HOURS`、`AUTH_MOCK` 均已存在 |

**当前不存在任何注册相关代码**（grep `register|signup|注册` 仅命中无关的 agent registry）。

### `/login` 返回前端的成功结构（register 须与之完全一致）

```json
{
  "code": 200, "msg": "success",
  "data": {
    "verification": true,
    "token": "<本后端生成的 JWT>",
    "token_type": "bearer",
    "expires_in": 43200,
    "user_info": { "user_id", "user_name", "department", "role" },
    "agent_access": [{"id": <agent code>, "name": <agent name>}],
    "skills_blacklist": [<skill entries>]
  }
}
```

> 注意：`/login` 路由第 63 行用 `permissions["agent_whitelist"]`、第 64 行用 `permissions["skills_blacklist"]`（复数 skills）。而 `_MOCK_USERS` 与 DAO 文档注释里用的是 `skill_blacklist`（单数 skill）。这是一处既有的潜在不一致。本计划**不修复该问题**（超出范围），而是保证 register 与 login 的取字段方式完全一致——通过抽取共享函数实现。

## 拟定改动

### 1. `app/models/auth.py` — 新增注册请求模型

新增 `RegisterRequest`，字段与 `LoginRequest` 相同但语义独立（便于 OpenAPI 文档区分）：

```python
class RegisterRequest(BaseModel):
    username: str
    password: str
```

### 2. `app/dao/user_dao.py` — 新增 mng 注册调用 + mock 支持

新增 `register_via_mng(username, password)`，**镜像** `verify_login_via_mng` 的实现，仅改两处：
- 请求 URL：`{MNG_URL}/api/auth/register`
- 失败时附带 mng 的 `message`（便于前端提示「用户名已存在」等）

```python
async def register_via_mng(username: str, password: str) -> dict:
    """调用 mng 管理中心注册。
    请求 POST {MNG_URL}/api/auth/register, body: {"username", "password"}
    成功时把 mng 返回标准化为与登录一致的内部结构。
    失败时返回 {"verification": False, "message": <mng message 或默认>}。
    """
    if not MNG_URL:
        logger.error("[user_dao] MNG_URL 未配置，无法调用 mng 注册")
        return {"verification": False, "message": "MNG_URL 未配置"}

    url = f"{MNG_URL}/api/auth/register"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"username": username, "password": password})
            if resp.status_code != 200:
                logger.warning(f"[user_dao] mng 注册返回非 200: {resp.status_code}")
                return {"verification": False, "message": "注册失败"}
            body = resp.json()
            if body.get("code") != 200:
                msg = body.get("message", "注册失败")
                logger.warning(f"[user_dao] mng 注册业务失败: {msg}")
                return {"verification": False, "message": msg}
            data = body.get("data", {}) or {}
            return {
                "verification": True,
                "user_info": data.get("user_info", {}),
                "access_token": data.get("access_token", ""),
                "permissions": data.get("permissions", {}),
            }
    except Exception as e:
        logger.exception(f"[user_dao] 调用 mng 注册服务失败: {e}")
        return {"verification": False, "message": "注册服务异常"}
```

新增 `register(username, password)`，**镜像** `verify_login` 的 `AUTH_MOCK` 开关逻辑。mock 模式下模拟一个注册成功的新用户（空权限，符合新账号常态）：

```python
async def register(username: str, password: str) -> dict:
    """注册用户。AUTH_MOCK=true 时返回模拟新用户；否则调用 mng 注册。"""
    if os.getenv("AUTH_MOCK", "true").lower() == "true":
        # 模拟注册成功：返回新账号结构（空权限）
        return {
            "verification": True,
            "user_info": {
                "user_id": username,
                "user_name": username,
                "department": "",
                "role": "普通用户",
            },
            "access_token": f"mock-access-token-{username}",
            "permissions": {"agent_whitelist": [], "skills_blacklist": []},
        }
    return await register_via_mng(username, password)
```

> 说明：mock 模式下不校验用户名是否重复，保持本地开发简单；真实重复检测由 mng 负责。

### 3. `app/routes/auth.py` — 新增 `/register` 路由 + 抽取共享成功处理函数

为保证 register 与 login 的成功返回**逐字段一致**，把 login 中「存 Redis → 生成 JWT → 构造响应」这段逻辑抽成共享函数 `_build_auth_success(result, request)`，login 与 register 共同复用：

```python
def _build_auth_success(result: dict, request: Request) -> dict:
    """登录/注册成功后的统一处理：存 Redis 权限 → 生成 JWT → 构造前端响应。"""
    user_info = result.get("user_info", {}) or {}
    user_id = user_info.get("user_id")
    access_token = result.get("access_token", "")
    permissions = result.get("permissions", {}) or {}

    if user_id:
        redis_client = getattr(request.app.state, "redis_client", None)
        if redis_client is not None:
            try:
                await save_user_permissions(redis_client, user_id, access_token, permissions)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    f"[auth] 保存用户 {user_id} 权限到 Redis 失败"
                )

    token_payload = {
        "user_id": user_id,
        "user_name": user_info.get("user_name", ""),
        "department": user_info.get("department", ""),
        "role": user_info.get("role", ""),
    }
    token = create_access_token(token_payload)
    return success_response({
        "verification": True,
        "token": token,
        "token_type": "bearer",
        "expires_in": JWT_EXPIRE_HOURS * 3600,
        "user_info": user_info,
        "agent_access": [{"id": d["code"], "name": d["name"]} for d in permissions["agent_whitelist"]],
        "skills_blacklist": permissions["skills_blacklist"],
    })
```

> 注：`_build_auth_success` 是 `async`（因 `save_user_permissions` 是 awaitable），需声明为 `async def`。

把现有 `login` handler 的成功分支替换为 `return await _build_auth_success(result, request)`（保持行为不变，仅是提取重构）。

新增 register handler：

```python
@router.post("/register")
async def register(request: Request, register_req: RegisterRequest):
    result = await register_req_dao(register_req.username, register_req.password)
    if not result.get("verification"):
        return error_response(400, result.get("message", "注册失败"))
    return await _build_auth_success(result, request)
```

其中 `register_req_dao` 为从 `app.dao.user_dao` 导入的 `register` 函数（import 时改名避免与路由函数同名冲突）：

```python
from app.dao.user_dao import verify_login, register as register_user
from app.models.auth import LoginRequest, RegisterRequest
```

路由内调用 `register_user(...)`。

### 4. 其他文件

- `services/auth_service.py`：**无需改动**，直接复用 `create_access_token` 与 `save_user_permissions`。
- `main.py`：**无需改动**，`auth.router` 已挂载，新增的 `/register` 自动生效。
- `config.py` / `.env`：**无需改动**，`MNG_URL`、`JWT_*`、`AUTH_MOCK` 均已存在。

## 改动文件清单

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `app/models/auth.py` | 新增 | `RegisterRequest` 模型 |
| `app/dao/user_dao.py` | 新增 | `register_via_mng`、`register` 两个函数 |
| `app/routes/auth.py` | 新增+重构 | 抽取 `_build_auth_success`；`login` 复用之；新增 `/register` 路由 |

## 假设与决策

1. **mng 注册响应契约**：与用户提供的样例一致 —— `{code:200, message:"注册成功", data:{user_info, access_token, token_type:"bearer", permissions:{}}}`。其中 `token_type` 不被使用（本后端响应里硬编码 `bearer`），与 login 处理方式一致。
2. **返回字段一致性**：register 成功返回与 `/login` **逐字段相同**，通过共享函数 `_build_auth_success` 保证。因此 login 中 `permissions["skills_blacklist"]`（复数）的取字段方式在 register 中同样沿用，**不在本次修复**该既有不一致（超范围）。
3. **mock 模式**：register 同样尊重 `AUTH_MOCK` 开关，与 login 行为对齐；mock 下返回空权限的新账号，便于本地联调。
4. **重复用户名检测**：由 mng 负责；mng 返回 `code!=200` 时，本后端返回 `error_response(400, <mng message>)`，前端据此提示。
5. **失败状态码**：login 失败用 401（凭证错误）；register 失败用 400（业务失败），并用 mng 的 `message` 作为提示文案。
6. **重构 login**：仅提取成功分支为共享函数，login 行为不变。此重构是为满足「返回与 /login 相同」的硬性要求而做的最小抽取，非额外功能。

## 验证步骤

1. **启动服务**：`AUTH_MOCK=true` 时本地启动后端。
2. **mock 注册**：`POST /register` body `{"username":"newuser","password":"123456"}` → 期望 `code:200`，`data` 含 `token`、`token_type:"bearer"`、`expires_in`、`user_info`、`agent_access:[]`、`skills_blacklist:[]`，字段集与 `/login` 一致。
3. **JWT 校验**：用返回的 `token` 调用受 `current_user` 保护的接口（如 `/chat`），应能正常解析 `user_id` 等。
4. **Redis 校验**：注册后用 `redis-cli GET user_permissions:<user_id>` 应得到含 `access_token` 与 `permissions` 的 JSON。
5. **真实 mng 联调**（`AUTH_MOCK=false`）：向真实 mng 发起注册，验证成功路径与失败路径（重复用户名 → `code:400` + mng message）。
6. **登录回归**：确认重构后 `POST /login` 返回结构未变（mock 账号 `admin/123456` 仍正常）。
