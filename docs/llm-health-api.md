# 大模型服务探测接口

## 接口说明

探测大模型网关是否存活。接口收到请求后，会向当前配置的默认模型（`models.default`）端点发送一次最小请求（`ping`，10 秒超时）。探测请求携带**虚构的 api_key**（不使用真实凭证、不消耗配额），网关返回 401（拒绝虚构鉴权）即视为存活。

- **URL**：`GET /health/llm`
- **鉴权**：无需登录，无需携带任何 Header

## 请求示例

```bash
curl -s http://localhost:7010/health/llm
```

## 成功响应

网关存活时返回 HTTP **200**：

```json
{
  "status": "ok",
  "model": "deepseek-v4-flash",
  "base_url": "https://geekai.co/api/v1",
  "latency_ms": 823
}
```

虚构 key 被拒（401）时返回如上结构；网关未启用鉴权、模型实际回复时额外携带 `reply` 字段（模型回复文本）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | string | 固定为 `"ok"`，表示网关存活 |
| `model` | string | 探测的模型名称（来自 `model_config.yml` 的 `models.default` 配置） |
| `base_url` | string | 模型服务端点地址 |
| `latency_ms` | int | 本次探测请求的往返耗时（毫秒） |
| `reply` | string | （可选）模型回复文本，仅网关放行虚构鉴权时存在 |

## 失败响应

网关异常时返回 HTTP **非 200**，**状态码原样透传网关返回的状态码**（401 除外）；无法建立连接/超时等场景返回 502/504：

```json
{
  "status": "error",
  "model": "deepseek-v4-flash",
  "base_url": "https://geekai.co/api/v1",
  "error": {
    "status_code": 403,
    "type": "PermissionDeniedError",
    "message": "Error code: 403 - forbidden"
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | string | 固定为 `"error"` |
| `model` | string | 同成功响应 |
| `base_url` | string | 同成功响应 |
| `error.status_code` | int | 错误状态码，与 HTTP 响应状态码一致 |
| `error.type` | string | 错误类型标识，见下表 |
| `error.message` | string | 报错详情，为网关返回的原始报错信息 |

## 状态码汇总

| HTTP 状态码 | `error.type` | 场景 |
|---|---|---|
| 200 | - | 网关存活（虚构鉴权被拒 401，或网关放行并回复） |
| 403 / 404 / 429 / 500 等 | SDK 异常类名（如 `PermissionDeniedError`、`RateLimitError`、`InternalServerError`） | 网关对探测请求返回非 401 错误，原样透传 |
| 502 | `connection_error` | 无法连接模型服务（网络不通、DNS 失败等） |
| 504 | `timeout` | 探测请求超过 10 秒未响应 |
| 500 | `config_error` | 后端模型配置未加载（`models.default` 缺失） |

## 判定建议

- 只需判断 HTTP 状态码是否为 `200` 即可（`200` = 网关存活，非 `200` = 异常）
- 需要排查原因时读取 `error.status_code` 与 `error.message`
- 注意：`200` 只代表网关存活，不代表真实 key 有效或配额充足（探测不携带真实凭证）
