# 新增 refresh_token 续期机制

## Summary

新增 `/refresh` 接口，前端在 access token 过期后凭 refresh_token 换取新的 access token。`/login`（及 `/register`）响应新增 `refresh_token` 字段（基于登录时间签发的 JWT，默认 7 天有效）。refresh_token 采用**固定有效期**（不滚动刷新），过期后提示重新登录。refresh_token 通过**请求体**传递。完成后 push 到 `origin/trae/agent-5CYjia`。

## Current State Analysis（基于代码探索，绝对路径引用）

- **JWT 签发**：[auth_service.py:17-23](file:///workspace/app/services/auth_service.py) `create_access_token(payload, expire_hours=JWT_EXPIRE_HOURS)`，注入 `iat`/`exp`，用 `JWT_SECRET`+`JWT_ALGORITHM`（HS256）。`decode_access_token`（26-28 行）直接 `jwt.decode`，异常透传。
- **配置**：[config.py:26-29](file:///workspace/app/config.py) `JWT_ALGORITHM="HS256"`、`JWT_SECRET`、`JWT_EXPIRE_HOURS=int(os.getenv("JWT_EXPIRE_HOURS","8"))`。新增配置沿用 `int(os.getenv(...))` 模式。
- **登录响应**：[auth.py _build_auth_success:38-95](file:///workspace/app/routes/auth.py) 返回 `success_response({...})` 裸 dict（无 response_model），data 内含 `token/token_type/expires_in/user_info/agent_access/skill_blacklist`。`/login`（98-103 行）与 `/register`（106-116 行）共用此函数。`_build_update_response`（130-169 行）重签 JWT 时也返回同构结构。
- **头解析模式**：[auth.py _require_jwt_from_header:119-127](file:///workspace/app/routes/auth.py) 与 [dependencies.py current_user:9-19](file:///workspace/app/dependencies.py) 均用 `authorization.lower().startswith("bearer ")` + `split(" ",1)[1].strip()`，异常映射 `ExpiredSignatureError`→401"登录已过期" / `InvalidTokenError`→401"登录凭证无效"。
- **无现有 refresh**：grep 确认全代码库无 refresh 机制。
- **模型**：[models/auth.py](file:///workspace/app/models/auth.py) 有 `LoginRequest` 等，但 `/login` 未挂 response_model，模型仅类型定义。新增 `RefreshRequest` 遵循同模式。

## Assumptions & Decisions

1. **refresh_token 传递**：请求体 `{"refresh_token": "..."}`（用户确认），新增 `RefreshRequest` Pydantic 模型。
2. **固定有效期**：/refresh 只返回新 access token，**不返回新 refresh_token**（用户确认）。7 天后必须重新登录。
3. **JWT 隔离**：refresh_token payload 加 `"type": "refresh"` 字段；access token 不加（或保持现状）。`decode_refresh_token` 解码后校验 `type=="refresh"`，防止 refresh_token 被当作 access token 用、反之亦然。为对称，access token 也加 `"type": "access"`，并在 `current_user` 校验——但这会改动现有 `current_user`/`create_access_token`，影响面大。**决定**：仅在 refresh token 加 `"type":"refresh"`，access token 保持不变；`/refresh` 解码时校验 type。access token 无法防被当 refresh 用，但 /refresh 端点要求 type=refresh，access token（无 type 字段）会被拒绝。简单且达成隔离。
4. **refresh_token payload**：与 access token 同构（user_id/username/name/department/role），便于 /refresh 直接复用 payload 签新 access token，无需查 Redis。
5. **/refresh 响应结构**：返回与登录一致的结构（含 user_info/agent_access/skill_blacklist），需从 Redis 取 permissions（复用 `get_user_permissions`）。permissions 失效（Redis 过期）→ 401"用户登录态已过期，请重新登录"。这样前端只需一种响应解析。
6. **_build_auth_success 改动**：签 access token 后追加签 refresh_token，响应 dict 加 `refresh_token` + `refresh_expires_in`。/register 共用，自动带上。
7. **配置项**：`JWT_REFRESH_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_EXPIRE_DAYS", "7"))`。
8. **Redis permissions TTL**：当前 `_PERMISSIONS_TTL = JWT_EXPIRE_HOURS * 3600`（8h），与 access token 同。refresh 续期时若 Redis 已过期（>8h 未活动），/refresh 返回 401 要求重登——这是合理行为（长期未活动应重登），不改 TTL。
9. **不动 _build_update_response**：3 个 update 端点重签 access token 时不刷新 refresh_token（refresh_token 仍有效，无需变动）。update 响应不加 refresh_token 字段（保持现状，避免前端误以为 refresh_token 变了）。
10. **错误响应**：refresh_token 过期→401"refresh_token 已过期，请重新登录"；无效→401"refresh_token 无效"；Redis 无权限→401"用户登录态已过期，请重新登录"。

## Proposed Changes

### 1. 配置 — [app/config.py](file:///workspace/app/config.py)

第 29 行 `JWT_EXPIRE_HOURS = ...` 之后追加：
```python
JWT_REFRESH_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_EXPIRE_DAYS", "7"))
```

### 2. JWT 签发/解码 — [app/services/auth_service.py](file:///workspace/app/services/auth_service.py)

import 改：`from app.config import JWT_ALGORITHM, JWT_SECRET, JWT_EXPIRE_HOURS, JWT_REFRESH_EXPIRE_DAYS`

在 `decode_access_token`（28 行）之后新增：
```python
def create_refresh_token(payload: dict, expire_days: int = JWT_REFRESH_EXPIRE_DAYS) -> str:
    """生成 refresh JWT, 默认按 JWT_REFRESH_EXPIRE_DAYS 过期（天）。"""
    now = datetime.now(timezone.utc)
    body = payload.copy()
    body["iat"] = int(now.timestamp())
    body["exp"] = int((now + timedelta(days=expire_days)).timestamp())
    body["type"] = "refresh"
    return jwt.encode(body, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_refresh_token(token: str) -> dict:
    """解析 refresh JWT; 非 refresh 类型或过期/签名错误时抛异常。"""
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    if payload.get("type") != "refresh":
        raise jwt.InvalidTokenError("非 refresh token")
    return payload
```

### 3. 登录响应加 refresh_token — [app/routes/auth.py](file:///workspace/app/routes/auth.py)

import 加 `create_refresh_token`、`JWT_REFRESH_EXPIRE_DAYS`。

`_build_auth_success` 第 55 行 `token = create_access_token(token_payload)` 之后加：
```python
    refresh_token = create_refresh_token(token_payload)
```
返回 dict（第 87-95 行）加两个字段：
```python
        "refresh_token": refresh_token,
        "refresh_expires_in": JWT_REFRESH_EXPIRE_DAYS * 86400,
```

### 4. 新增 /refresh 端点 — [app/routes/auth.py](file:///workspace/app/routes/auth.py)

新增 import `decode_refresh_token`。新增模型 `RefreshRequest`（在 models/auth.py）。在文件末尾（3 个 update 端点之后）新增：
```python
@router.post("/refresh")
async def refresh_token(request: Request, body: RefreshRequest):
    """凭 refresh_token 换取新的 access token（固定有效期，不滚动刷新）。"""
    try:
        payload = decode_refresh_token(body.refresh_token)
    except jwt.ExpiredSignatureError:
        return error_response(401, "refresh_token 已过期，请重新登录")
    except jwt.InvalidTokenError:
        return error_response(401, "refresh_token 无效")

    user_id = payload.get("user_id")
    if not user_id:
        return error_response(401, "refresh_token 无效")

    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis 未就绪")
    perms_data = await get_user_permissions(redis_client, user_id)
    if not perms_data:
        return error_response(401, "用户登录态已过期，请重新登录")
    permissions = perms_data.get("permissions", {}) or {}

    token_payload = {
        "user_id": user_id,
        "username": payload.get("username", ""),
        "name": payload.get("name", ""),
        "department": payload.get("department", ""),
        "role": payload.get("role", ""),
    }
    token = create_access_token(token_payload)
    user_info = {
        "id": user_id,
        "username": token_payload["username"],
        "name": token_payload["name"],
        "department": token_payload["department"],
        "role": token_payload["role"],
    }
    return success_response({
        "verification": True,
        "token": token,
        "token_type": "bearer",
        "expires_in": JWT_EXPIRE_HOURS * 3600,
        "user_info": user_info,
        "agent_access": permissions.get("agent_whitelist", []),
        "skill_blacklist": permissions.get("skill_blacklist", []),
    })
```
> 不返回 refresh_token（固定有效期，原 refresh_token 继续用直到 7 天过期）。

### 5. 新增模型 — [app/models/auth.py](file:///workspace/app/models/auth.py)

在 `UpdatePasswordRequest` 之后（或 `UserInfo` 之前）加：
```python
class RefreshRequest(BaseModel):
    refresh_token: str
```

## Verification

1. **静态检查**：`python -m py_compile app/config.py app/services/auth_service.py app/routes/auth.py app/models/auth.py` 全部通过。
2. **grep 核对**：`create_refresh_token`/`decode_refresh_token` 在 auth_service.py 定义、auth.py 调用；`/refresh` 端点存在；`RefreshRequest` 在 models 定义并被 import；`JWT_REFRESH_EXPIRE_DAYS` 在 config 定义并被引用。
3. **行为核对**：
   - /login 响应 data 含 `refresh_token` + `refresh_expires_in`（7×86400=604800）。
   - /refresh 正常：body 传有效 refresh_token → 200，data 含新 `token`（access，8h），不含 refresh_token。
   - /refresh refresh_token 过期 → 401"refresh_token 已过期，请重新登录"。
   - /refresh refresh_token 无效/被篡改 → 401"refresh_token 无效"。
   - /refresh 用 access token 冒充（无 type=refresh）→ 401"refresh_token 无效"。
   - /refresh Redis permissions 过期 → 401"用户登录态已过期，请重新登录"。
   - /register 响应也带 refresh_token（共用 _build_auth_success）。
4. **push**：commit + push 到 `origin/trae/agent-5CYjia`。

## 执行步骤

1. 编辑 `app/config.py`（加 JWT_REFRESH_EXPIRE_DAYS）
2. 编辑 `app/services/auth_service.py`（加 create_refresh_token/decode_refresh_token）
3. 编辑 `app/models/auth.py`（加 RefreshRequest）
4. 编辑 `app/routes/auth.py`（_build_auth_success 加 refresh_token；新增 /refresh 端点）
5. `python -m py_compile` 四文件 + grep 核对
6. `git add` + `git commit` + `git push`
