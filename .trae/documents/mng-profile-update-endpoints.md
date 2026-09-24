# 新增修改姓名/部门/密码接口 + mng access_token 透传

## Summary

在 auth 路由新增 3 个需登录的接口（PUT）：修改姓名、修改部门、修改密码。每个接口都从 Redis 取出当前用户的 mng `access_token`，以 `Authorization: Bearer {token}` 调用对应 mng 外部接口；成功后按规则重建登录格式的响应返回前端。其中改密码走真实重登流程，改姓名/部门走本地重建（因当前用户密码不可用，无法真正调 mng 登录）。完成后 push 到远程分支 `trae/agent-5CYjia`。

## Current State Analysis（基于代码探索，均为绝对路径）

1. **登录与 mng 验证**：`/login`（[auth.py:83](file:///workspace/app/routes/auth.py)）→ `verify_login`（[user_dao.py:110](file:///workspace/app/dao/user_dao.py)）→ `AUTH_MOCK=false` 时 `verify_login_via_mng`（[user_dao.py:71](file:///workspace/app/dao/user_dao.py)）调 `POST {MNG_AUTH_URL}/api/auth/user/login`，body `{"username","password"}`，**无 Authorization 头**（登录本身是换 token，合理）。mng 返回 `{code, message, data}`，`data` 含 `user_info / access_token / permissions`（[user_dao.py:98-104](file:///workspace/app/dao/user_dao.py)）。

2. **access_token 已存 Redis**：`_build_auth_success`（[auth.py:24-80](file:///workspace/app/routes/auth.py)）提取 `access_token`，经 `save_user_permissions`（[auth_service.py:31-48](file:///workspace/app/services/auth_service.py)）写入 `user_permissions:{user_id}`，value=`{"access_token","permissions"}`，TTL=`JWT_EXPIRE_HOURS*3600`。**需求 #1 的"存 Redis"已实现。**

3. **现有 mng 调用已带 Bearer**：`fetch_external_intents`（[mng_service.py:54-58](file:///workspace/app/services/mng_service.py)）与 `mng_proxy`（[mng_proxy.py:33-37,46-50](file:///workspace/app/routes/mng_proxy.py)）均发送 `Authorization: Bearer {access_token}`，token 通过 `get_user_permissions(redis_client, user_id)`（[auth_service.py:51-67](file:///workspace/app/services/auth_service.py)）从 Redis 取。`mng_proxy._get_access_token`（[mng_proxy.py:11-25](file:///workspace/app/routes/mng_proxy.py)）封装了"JWT→user_id→Redis→access_token（缺失抛 401）"模式。**需求 #1 的"每次调用 mng 都带 Bearer"对现有流程已成立；新接口沿用同一模式即可。**

4. **JWT payload**（[auth.py:65-70](file:///workspace/app/routes/auth.py)）：`{user_id, user_name, department, role}`。**用户已确认：JWT 里的 `user_name` 就是登录账号 username**（mock 数据把 `user_name` 写成"小张"是 mock 不准，以用户说明为准）。故改密码重登可用 `verify_login(user["user_name"], new_password)`。

5. **登录响应结构**（[auth.py:72-80](file:///workspace/app/routes/auth.py)）：`{verification, token, token_type, expires_in, user_info, agent_access, skill_blacklist}`，外包 `{code:200, msg, data}`（`success_response`，[auth.py:16-17](file:///workspace/app/routes/auth.py)）。`_build_auth_success` 内部会 `save_user_permissions` + `build_and_cache_user_config`（后者触发 `fetch_external_intents` 调 mng，属真实登录流程的一部分）。

6. **模型**（[models/auth.py](file:///workspace/app/models/auth.py)）：仅有 `LoginRequest / RegisterRequest / UserInfo / LoginResponse`，无更新类模型。

7. **路由装配**（[main.py:128](file:///workspace/app/main.py)）：`app.include_router(auth.router, tags=["auth"])`，**无前缀**，故 `/login`、`/register` 挂在根路径。新接口同样挂根路径。

8. **鉴权依赖**（[dependencies.py:9-19](file:///workspace/app/dependencies.py)）：`current_user` 解析 `Authorization: Bearer <JWT>` 返回 payload dict（含 `user_id/user_name/department/role`）。

## Assumptions & Decisions

1. **需求 #1 无独立代码改动**：access_token 落 Redis 与现有 mng 调用带 Bearer 均已实现；新接口沿用 `get_user_permissions` + `Authorization: Bearer` 模式即可。
2. **改姓名/部门 → 本地重建**（用户已选）：不调 mng 登录、不调 mng GET /me。复用 Redis 中 `access_token`+`permissions`，基于 JWT 字段重建 `user_info` 并应用本次修改，重签 JWT、刷新 Redis 权限 TTL，返回与登录一致的结构。
3. **改密码 → 真实重登**（用户要求）：mng PUT 成功后用 `verify_login(user["user_name"], new_password)` 走完整登录流程（含 mng 登录 + 重新融合意图 + 落 Redis + 新 JWT），复用 `_build_auth_success`。
4. **"姓名"字段 = `name`**：用户名(登录)=`user_name`，姓名≠用户名；mng 注册接口（[user_dao.py:145](file:///workspace/app/dao/user_dao.py)）以 `name` 表示姓名，`/me/name` 接口入参亦为 `name`，故本地重建时 `user_info` 增/改 `name` 字段。`user_name`（登录账号）不被覆盖。
5. **JWT 不新增字段**：`name` 仅出现在响应 `user_info`，不入 JWT（`current_user` 下游只用 `user_id`，无需 `name`）。改部门时重签 JWT 会更新其中的 `department`。
6. **AUTH_MOCK 模式**：3 个新 mng PUT 函数不做 mock 分支（`MNG_AUTH_URL` 为空时返回失败）。mock 模式下这些接口不可用，符合"聚焦真实 mng 集成"且避免过度设计。
7. **mng PUT 响应解析**：沿用现有约定——HTTP 200 且 `body.code==200` 视为成功，否则取 `body.message` 作为失败信息。
8. **路径**：3 个接口路径与 mng 镜像（`/api/auth/me/name` 等），挂根路径，与现有 `/api/presentation/*`（mng_proxy）无冲突（不同前缀）。
9. **不引入新依赖**：复用 `httpx`、`pydantic`、现有 `auth_service` / `user_dao` / `current_user`。

## Proposed Changes

### 1. 新增请求模型 — [app/models/auth.py](file:///workspace/app/models/auth.py)
在 `RegisterRequest` 之后新增 3 个模型：
```python
class UpdateNameRequest(BaseModel):
    name: str

class UpdateDepartmentRequest(BaseModel):
    department: str

class UpdatePasswordRequest(BaseModel):
    old_password: str
    new_password: str
```

### 2. 新增 mng PUT DAO 函数 — [app/dao/user_dao.py](file:///workspace/app/dao/user_dao.py)
在 `register` 之后新增 3 个异步函数，均用 `httpx.AsyncClient(timeout=10.0)` 发 `PUT`，headers 带 `{"Authorization": f"Bearer {access_token}"}`，沿用 `verify_login_via_mng` 的状态码/`code` 解析风格（HTTP 非 200 或 `body.code!=200` 视为失败，返回 `{"success": False, "message": <mng message 或默认>}`；成功返回 `{"success": True}`）。`MNG_AUTH_URL` 未配置时返回 `{"success": False, "message": "MNG_AUTH_URL 未配置"}`。

- `update_name_via_mng(access_token: str, name: str) -> dict`：`PUT {MNG_AUTH_URL}/api/auth/me/name`，json `{"name": name}`
- `update_department_via_mng(access_token: str, department: str) -> dict`：`PUT {MNG_AUTH_URL}/api/auth/me/department`，json `{"department": department}`
- `update_password_via_mng(access_token: str, old_password: str, new_password: str) -> dict`：`PUT {MNG_AUTH_URL}/api/auth/me/password`，json `{"old_password": old_password, "new_password": new_password}`

### 3. 新增 3 个路由 + 2 个私有 helper — [app/routes/auth.py](file:///workspace/app/routes/auth.py)

**新增 import**：
```python
from fastapi import APIRouter, HTTPException, Request, Depends
from app.dependencies import current_user
from app.services.auth_service import create_access_token, save_user_permissions, get_user_permissions
from app.dao.user_dao import (
    verify_login, register as register_user,
    update_name_via_mng, update_department_via_mng, update_password_via_mng,
)
from app.models.auth import (
    LoginRequest, RegisterRequest,
    UpdateNameRequest, UpdateDepartmentRequest, UpdatePasswordRequest,
)
```

**helper A — `_require_mng_access_token(request, user) -> str`**：镜像 `mng_proxy._get_access_token`（[mng_proxy.py:11-25](file:///workspace/app/routes/mng_proxy.py)）。从 `user["user_id"]` + `request.app.state.redis_client` 调 `get_user_permissions` 取 `access_token`；缺失抛 401（"用户登录态已过期，请重新登录"/"未找到 mng access_token，请重新登录"）。

**helper B — `_build_local_auth_success(request, user, updates) -> dict`**（改姓名/部门本地重建）：
1. `user_id = user["user_id"]`；取 `redis_client`；`perms_data = get_user_permissions(redis_client, user_id)`，None→401。
2. `access_token, permissions = perms_data["access_token"], perms_data.get("permissions", {})`。
3. 构造 `user_info = {"id": user_id, "user_name": user.get("user_name",""), "department": user.get("department",""), "role": user.get("role","")}`，再 `user_info.update(updates)`（姓名→`{"name": body.name}`；部门→`{"department": body.department}`）。
4. `save_user_permissions(redis_client, user_id, access_token, permissions)` 刷新 TTL（失败仅记日志，不阻断）。
5. 重签 JWT：`token_payload = {user_id, user_name, department(可能已更新), role}` → `create_access_token`。
6. 返回 `success_response({verification:True, token, token_type:"bearer", expires_in:JWT_EXPIRE_HOURS*3600, user_info, agent_access: permissions.get("agent_whitelist",[]), skill_blacklist: permissions.get("skill_blacklist",[])})`。

**3 个路由**（均 `user: dict = Depends(current_user)`）：
```python
@router.put("/api/auth/me/name")
async def update_name(request: Request, body: UpdateNameRequest, user: dict = Depends(current_user)):
    access_token = await _require_mng_access_token(request, user)
    result = await update_name_via_mng(access_token, body.name)
    if not result.get("success"):
        return error_response(400, result.get("message", "修改姓名失败"))
    return await _build_local_auth_success(request, user, {"name": body.name})

@router.put("/api/auth/me/department")
async def update_department(request: Request, body: UpdateDepartmentRequest, user: dict = Depends(current_user)):
    access_token = await _require_mng_access_token(request, user)
    result = await update_department_via_mng(access_token, body.department)
    if not result.get("success"):
        return error_response(400, result.get("message", "修改部门失败"))
    return await _build_local_auth_success(request, user, {"department": body.department})

@router.put("/api/auth/me/password")
async def update_password(request: Request, body: UpdatePasswordRequest, user: dict = Depends(current_user)):
    access_token = await _require_mng_access_token(request, user)
    result = await update_password_via_mng(access_token, body.old_password, body.new_password)
    if not result.get("success"):
        return error_response(400, result.get("message", "修改密码失败"))
    # 用 username(=JWT user_name) + 新密码走完整登录流程
    login_result = await verify_login(user.get("user_name", ""), body.new_password)
    if not login_result.get("verification"):
        return error_response(401, "修改密码成功但自动登录失败，请手动登录")
    return await _build_auth_success(login_result, request)
```

**不改**：`verify_login_via_mng`（登录本身不带 Authorization，合理）、`_build_auth_success`（改密码复用）、`mng_proxy`、`auth_service`、`config.py`、`main.py`。

## Verification

1. **静态检查**：`python -m py_compile app/models/auth.py app/dao/user_dao.py app/routes/auth.py` 全部通过。
2. **grep 核对**：3 个 DAO 函数（`update_name_via_mng`/`update_department_via_mng`/`update_password_via_mng`）在 user_dao.py 定义、在 auth.py 调用；`_require_mng_access_token`/`_build_local_auth_success` 在 auth.py 定义并调用；3 个路由均 `Depends(current_user)` 且 PUT 路径正确。
3. **Bearer 头核对**：3 个 DAO 函数的 `client.put` 均含 `headers={"Authorization": f"Bearer {access_token}"}`。
4. **行为核对**：
   - 改姓名/部门成功 → 本地重建（无 mng 登录调用），响应结构与登录一致，`user_info` 含更新字段。
   - 改密码成功 → `verify_login(user_name, new_password)` 真实重登 → `_build_auth_success`（重存 Redis + 重新融合 + 新 JWT）。
   - mng token 缺失 → 401；mng PUT 失败 → 400 带 message。
5. **push**：`git add` 改动文件 → `git commit` → `git push`（已设 upstream `origin/trae/agent-5CYjia`）。

## 执行步骤

1. 编辑 `app/models/auth.py`（3 模型）
2. 编辑 `app/dao/user_dao.py`（3 DAO 函数）
3. 编辑 `app/routes/auth.py`（import + 2 helper + 3 路由）
4. `python -m py_compile` 三文件 + grep 核对
5. `git add` + `git commit` + `git push`
