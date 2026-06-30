# mng URL 拆分 + 卡片按 userId 取 access_token 实现计划

## 摘要

两项改动：
1. **拆分 mng URL 配置**：将单一 `MNG_URL` 拆为 `MNG_AUTH_URL`（用户登录/注册）与 `MNG_INTENT_URL`（外部意图 + 卡片）两个独立配置项。
2. **卡片接口按 userId 从 redis 取 access_token**：为 `mng_proxy.py` 的两个卡片端点接入 JWT 鉴权（`Depends(current_user)`），从 JWT 解出 `user_id`，经 `get_user_permissions(redis_client, user_id)` 从 redis 取出 mng `access_token`，放入 `Authorization: Bearer` 请求头去请求 `MNG_INTENT_URL`，并移除既有的硬编码 mock 返回。

## 现状分析

经探索确认（均位于 `/workspace/app`）：

| 关注点 | 文件 | 现状 |
|---|---|---|
| mng URL 定义 | `config.py:51` | `MNG_URL = os.getenv("MNG_URL", "")`，单一地址 |
| 用户登录/注册 | `dao/user_dao.py` | `verify_login_via_mng`(L81) → `{MNG_URL}/api/auth/login`；`register_via_mng`(L140) → `{MNG_URL}/api/auth/register` |
| 外部意图获取 | `services/mng_service.py` | `fetch_external_intents(access_token)`(L18) → `GET {MNG_URL}/api/intents`，header 带 `Authorization: Bearer {access_token}` |
| 卡片代理 | `routes/mng_proxy.py` | `GET /api/ui/presentation/cards` 与 `GET /api/ui/presentation/custom-components`，**完全 mock、无鉴权、无 userId、不带 access_token**；httpx 调 mng 的代码因前面 `return` 是死代码 |
| redis 取 token | `services/auth_service.py` | `get_user_permissions(redis_client, user_id)`(L51) 返回 `{"access_token","permissions"}` 或 `None` |
| 意图调用链 | `services/orchestrator_service.py:218-234` | 已有模式：`user_id`(来自 JWT) → `get_user_permissions` → `access_token` → `fetch_external_intents(access_token)`。**意图获取已符合"按 userId 从 redis 取 token"要求，本次不改其逻辑，仅切换 URL 配置** |
| userId 来源 | `dependencies.py` | `current_user(authorization)` 从 `Authorization: Bearer <JWT>` 解析，返回含 `user_id` 的 dict；`/chat`、`/sessions`、`/upload`、`/feedback` 均用此依赖 |

**`MNG_URL` 的全部代码引用**（文档/spec 文件不计）：
- `config.py:51`（定义）
- `dao/user_dao.py:24,77,81,136,140`（用户鉴权）
- `services/mng_service.py:10,45,52`（意图）
- `routes/mng_proxy.py:4,41,44,51,54`（卡片）

## 拟定改动

### 1. `app/config.py` — 拆分配置项

将 L50-51 的 `MNG_URL` 替换为两个独立配置项：

```python
# 管理中心 - 用户鉴权（登录/注册）地址
MNG_AUTH_URL = os.getenv("MNG_AUTH_URL", "")
# 管理中心 - 意图与卡片地址
MNG_INTENT_URL = os.getenv("MNG_INTENT_URL", "")
```

> 不保留 `MNG_URL`（用户选择全新拆分）。两个配置项独立，互不影响。

### 2. `.env` 与 `.env.example` — 更新环境变量

**`.env`** L33-34：
```
# 管理中心配置
MNG_URL=http://localhost:7009
```
改为：
```
# 管理中心配置（用户鉴权 / 意图卡片 地址分离）
MNG_AUTH_URL=http://localhost:7009
MNG_INTENT_URL=http://localhost:7009
```

**`.env.example`** L33：
```
MNG_URL=
```
改为：
```
MNG_AUTH_URL=
MNG_INTENT_URL=
```

> 本地两个地址相同（都指向 localhost:7009），但语义独立，便于生产环境分别部署。

### 3. `app/dao/user_dao.py` — 切换到 `MNG_AUTH_URL`

- L24：`from app.config import MNG_URL` → `from app.config import MNG_AUTH_URL`
- `verify_login_via_mng`：
  - L77-78：`if not MNG_URL:` → `if not MNG_AUTH_URL:`，日志文案同步
  - L81：`url = f"{MNG_URL}/api/auth/login"` → `url = f"{MNG_AUTH_URL}/api/auth/login"`
- `register_via_mng`：
  - L136-137：`if not MNG_URL:` → `if not MNG_AUTH_URL:`，日志文案同步
  - L140：`url = f"{MNG_URL}/api/auth/register"` → `url = f"{MNG_AUTH_URL}/api/auth/register"`

### 4. `app/services/mng_service.py` — 切换到 `MNG_INTENT_URL`

- L10：`from app.config import MNG_URL` → `from app.config import MNG_INTENT_URL`
- `fetch_external_intents`：
  - L45-46：`if not MNG_URL:` → `if not MNG_INTENT_URL:`，日志文案同步
  - L52：`url = f"{MNG_URL}{_MNG_INTENTS_PATH}"` → `url = f"{MNG_INTENT_URL}{_MNG_INTENTS_PATH}"`

> `fetch_external_intents` 的签名与 access_token 传递逻辑**不变**——它已由调用方（orchestrator）从 redis 取 token 后传入，符合要求。本次仅切换 URL 配置。

### 5. `app/routes/mng_proxy.py` — 接入鉴权 + redis 取 token + 真实转发 mng

重写两个端点：接入 `Depends(current_user)` 获取 `user_id` → 从 `request.app.state.redis_client` 取 `access_token` → 放入 `Authorization: Bearer` 头请求 `MNG_INTENT_URL`。移除硬编码 mock。

完整新文件内容：

```python
from fastapi import APIRouter, Depends, HTTPException, Request
import httpx

from app.config import MNG_INTENT_URL
from app.dependencies import current_user
from app.services.auth_service import get_user_permissions

router = APIRouter()


async def _get_access_token(request: Request, user: dict) -> str:
    """从 redis 按 user_id 取 mng access_token；取不到则抛 401。"""
    user_id = user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="token 中缺少 user_id")
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis 未就绪")
    perms_data = await get_user_permissions(redis_client, user_id)
    if not perms_data:
        raise HTTPException(status_code=401, detail="用户登录态已过期，请重新登录")
    access_token = perms_data.get("access_token", "")
    if not access_token:
        raise HTTPException(status_code=401, detail="未找到 mng access_token，请重新登录")
    return access_token


@router.get("/api/ui/presentation/cards")
async def proxy_card_configs(request: Request, user: dict = Depends(current_user)):
    if not MNG_INTENT_URL:
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    access_token = await _get_access_token(request, user)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{MNG_INTENT_URL}/ui/presentation/cards",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return resp.json()


@router.get("/api/ui/presentation/custom-components")
async def proxy_custom_component_configs(request: Request, user: dict = Depends(current_user)):
    if not MNG_INTENT_URL:
        raise HTTPException(status_code=500, detail="MNG_INTENT_URL not configured")
    access_token = await _get_access_token(request, user)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{MNG_INTENT_URL}/ui/presentation/custom-components",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return resp.json()
```

要点：
- **鉴权**：`Depends(current_user)` 复用现有 JWT 依赖，与 `/chat`、`/sessions` 一致；前端只需在 `Authorization` 头带 JWT，无需额外传 userId。
- **取 token**：`_get_access_token` 抽取为共享辅助函数，两个端点复用；取不到/过期统一返回 401。
- **转发 mng**：移除硬编码 mock，真实 `GET {MNG_INTENT_URL}/ui/presentation/cards`（与 `/custom-components`），携带 `Authorization: Bearer {access_token}`。
- **路径**：保留原有 mng 路径 `/ui/presentation/cards`（不带 `/api` 前缀，与原死代码一致）；FastAPI 路由路径 `/api/ui/...` 不变，前端无感。
- **错误透传**：mng 返回的 JSON 原样返回（与原死代码行为一致）；若需对 mng 非 200 做处理可后续再加，本次不做。

## 改动文件清单

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `app/config.py` | 替换 | `MNG_URL` → `MNG_AUTH_URL` + `MNG_INTENT_URL` |
| `.env` | 替换 | `MNG_URL` → `MNG_AUTH_URL` + `MNG_INTENT_URL` |
| `.env.example` | 替换 | `MNG_URL` → `MNG_AUTH_URL` + `MNG_INTENT_URL` |
| `app/dao/user_dao.py` | 引用替换 | `MNG_URL` → `MNG_AUTH_URL`（登录+注册） |
| `app/services/mng_service.py` | 引用替换 | `MNG_URL` → `MNG_INTENT_URL`（意图） |
| `app/routes/mng_proxy.py` | 重写 | 接入鉴权 + redis 取 token + 真实转发 mng，移除 mock |

## 假设与决策

1. **配置命名**：`MNG_AUTH_URL`（用户登录/注册）+ `MNG_INTENT_URL`（意图+卡片），用户已确认。不保留 `MNG_URL`，全量替换。
2. **意图获取不改逻辑**：`fetch_external_intents` 已由 orchestrator 按 `user_id` 从 redis 取 `access_token` 后传入（`orchestrator_service.py:218-234`），符合"按 userId 从 redis 取 token 放 header"要求；本次仅把其 URL 配置从 `MNG_URL` 切到 `MNG_INTENT_URL`。不改其签名与调用方。
3. **userId 来源**：复用 JWT 鉴权（`Depends(current_user)`），用户已确认。前端在 `Authorization` 头带 JWT 即可，无需显式传 userId，与现有受保护路由一致，且不可伪造他人 userId。
4. **卡片接口移除 mock**：原 `mng_proxy.py` 的硬编码返回会被删除，改为真实请求 mng。本地联调若无 mng 卡片服务，接口会返回 mng 的真实响应或 httpx 错误（不再返回假数据）。
5. **mng 路径前缀**：转发到 mng 的路径保持 `/ui/presentation/cards` 与 `/ui/presentation/custom-components`（不带 `/api`），与原死代码一致；FastAPI 对外路由 `/api/ui/...` 不变。
6. **`MNG_INTENT_URL` 同时覆盖意图与卡片**：两者都属于"意图卡片"范畴，共用一个配置项（用户表述"获取意图卡片用一个"）。
7. **token 缺失返回 401**：redis 中无该用户权限数据（未登录/登录态过期）时返回 401，引导前端重新登录。

## 验证步骤

1. **配置加载**：启动后端，确认 `MNG_AUTH_URL`、`MNG_INTENT_URL` 均从 `.env` 正确读取。
2. **登录/注册回归**（`AUTH_MOCK=false`）：`POST /login`、`POST /register` 仍能正确调用 `{MNG_AUTH_URL}/api/auth/*`。
3. **意图获取回归**：`POST /chat` 触发 orchestrator，确认 `fetch_external_intents` 调用的是 `{MNG_INTENT_URL}/api/intents`，且 header 带 access_token。
4. **卡片接口鉴权**：
   - 不带 `Authorization` 头访问 `GET /api/ui/presentation/cards` → 401。
   - 带无效 JWT → 401。
   - 带有效 JWT 但 redis 无该 user 权限（未登录或过期）→ 401。
5. **卡片接口正常转发**：登录后用返回的 JWT 访问 `GET /api/ui/presentation/cards`，确认后端从 redis 取到 access_token 并以 `Authorization: Bearer` 转发到 `{MNG_INTENT_URL}/ui/presentation/cards`，返回 mng 真实响应。`/custom-components` 同理。
6. **配置独立验证**：把 `MNG_AUTH_URL` 与 `MNG_INTENT_URL` 设为不同地址，确认登录走前者、卡片/意图走后者，互不影响。
7. **语法/导入校验**：`py_compile` 全部改动文件；静态确认无残留 `MNG_URL` 引用（`grep -r "MNG_URL" app/` 应无结果）。
