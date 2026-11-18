# 制度问答 · 知识库选择与可见文档功能 — 前端对接文档

> 本次变更包含：登录响应新增字段、`/chat` 请求新增字段、两个全新接口（列文档 / 查看源文档）。
> 所有接口均需携带本系统 JWT：`Authorization: Bearer <token>`。旧版前端不传新字段时行为与之前完全一致（全选语义），可平滑升级。

---

## 一、登录 / 刷新响应变更（agent_access）

**涉及接口**：`POST /login`、`POST /register`、`POST /refresh`、`PUT /api/auth/me/name`、`PUT /api/auth/me/department`、`PUT /api/auth/me/password`

**变更点**：`agent_access` 中制度问答合并项（`id = "regulations_agent"`）新增 `knowledge_bases` 字段 —— 当前用户可访问的知识库名列表（多选选择器的选项来源）。

```json
"agent_access": [
  {
    "id": "regulations_agent",
    "name": "制度问答",
    "description": "根据公司内部制度文件知识库回答问题",
    "knowledge_bases": ["金科制度问答", "信科制度问答"]
  },
  {
    "id": "999",
    "name": "生成PPT智能体",
    "description": "用于生成精美ppt"
  }
]
```

**前端使用说明**：

- `knowledge_bases` 为空数组 `[]`：用户没有任何制度问答知识库权限，不渲染选择器，制度问答入口可隐藏或置灰
- `knowledge_bases` 只有 1 项：可以不渲染选择器（选不选等价），直接全选
- `knowledge_bases` ≥ 2 项：渲染**多选**选择器
- 未配置该智能体权限的用户：`agent_access` 中不会出现 `regulations_agent` 项（与之前一致）

---

## 二、POST /chat 请求变更

**变更点**：请求体新增可选字段 `knowledge_bases`（仅对制度问答生效）。

```json
{
  "messages": [...],
  "session_id": "xxx",
  "agent_id": "regulations_agent",
  "search_enabled": true,
  "knowledge_bases": ["金科制度问答", "信科制度问答"]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| knowledge_bases | string[] | 否 | 本轮问答限定的知识库名列表。**空数组 / 缺省 = 全选**（用户权限内全部知识库联合检索，即旧行为） |

**行为约定**：

- 传入选中库名 → 后端只在所选库中检索
- 所选库名不在用户权限内（如权限变更后登录态过期）→ 模型回复引导重新登录的提示
- **选择状态由前端管理**：用户在会话中切换选择后，后续每轮 `/chat` 请求都应持续携带当前选择，直到用户显式改回全选（传空数组或不传）

---

## 三、列文档接口（新增）

`POST /api/v1/policy/documents/query`

查询当前用户可见知识库中的文档列表（含摘要、示例问题），用于问答界面的文档列表展示。

**请求体**：

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| page | int | 否 | 1 | ≥ 1 |
| page_size | int | 否 | 20 | 1 ~ 100 |
| search | string | 否 | "" | 文件名模糊搜索 |
| knowledge_bases | string[] | 否 | [] | 与 `/chat` 选择器联动：空 = 全部权限内知识库；非空 = 只列所选库的文档 |

```json
{
  "page": 1,
  "page_size": 20,
  "search": "报销",
  "knowledge_bases": ["金科制度问答"]
}
```

**响应**（统一信封）：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "items": [
      {
        "doc_id": "doc_258c6f42670e421d",
        "kb_id": "123",
        "kb_name": "金科制度问答",
        "filename": "差旅费报销管理办法.pdf",
        "file_type": ".pdf",
        "file_size": 1024000,
        "page_count": 12,
        "summary": "本办法规定了差旅费的开支范围、报销标准与审批流程…",
        "example_questions": ["差旅报销标准是什么", "住宿费上限是多少"],
        "updated_at": "2026-10-15T08:30:00Z"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20
  }
}
```

**items 字段说明**：

| 字段 | 类型 | 说明 |
|---|---|---|
| doc_id | string | 文档唯一 ID，**查看源文档接口的入参** |
| kb_id | string | 所属知识库 ID |
| kb_name | string | 所属知识库名 |
| filename | string | 文件名（含扩展名） |
| file_type | string | 扩展名，如 `.pdf` / `.docx` |
| file_size | int | 字节数 |
| page_count | int | 总页数 |
| summary | string | 文档摘要（≤300 字） |
| example_questions | string[] | 推荐示例问题（≤3 条），可直接做成可点击的快捷提问 |
| updated_at | string | 更新时间（ISO 8601） |

**错误码**：

| 状态 | 场景 | 处理建议 |
|---|---|---|
| 401 | JWT 无效 / 用户登录态过期 | 引导重新登录 |
| 502 | 知识库服务查询失败 | 提示"文档列表查询失败，请稍后重试" |

**特殊响应**：用户无任何制度问答权限（或所选 knowledge_bases 与权限交集为空）时，接口正常返回 200 + `items: [], total: 0`（不算错误）。

---

## 四、查看源文档接口（新增）

`GET /api/v1/policy/documents/{doc_id}/file`

获取文档原始文件流（在线预览用），`doc_id` 来自列文档接口返回。

**请求**：

```
GET /api/v1/policy/documents/doc_258c6f42670e421d/file
Authorization: Bearer <token>
```

**响应（200）**：裸文件流，非 JSON 信封。

| 响应头 | 说明 |
|---|---|
| Content-Type | 按文件类型映射：`.pdf` → `application/pdf`；`.docx` → `application/vnd.openxmlformats-officedocument.wordprocessingml.document`；`.xlsx` → `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`；`.pptx` → `application/vnd.openxmlformats-officedocument.presentationml.presentation`；`.txt` → `text/plain; charset=utf-8`；`.md` → `text/markdown; charset=utf-8`；其他 → `application/octet-stream` |
| Content-Disposition | `inline; filename="<原文件名>"`（建议用于浏览器内预览） |
| X-Page-Count | 文档总页数（int 字符串），可用于预览页数展示 |

**错误码**：

| 状态 | 场景 | 处理建议 |
|---|---|---|
| 401 | JWT 无效 / 登录态过期 | 引导重新登录 |
| 404 | 文档不存在、无权查看、或源文件暂不可用（统一文案"文档不存在或您无权查看"） | 按文档不存在处理，可在列表中置灰该项 |
| 502 | 知识库服务异常 | 提示稍后重试 |

**安全说明**：后端会严格校验 `doc_id` 必须在当前用户权限内，前端无需（也不应）传 kb_id；越权访问统一返回 404。

---

## 五、典型交互流程

1. **登录** → 从响应 `agent_access` 中找 `regulations_agent` 项，读 `knowledge_bases` 渲染多选选择器（默认全不勾 = 全选）
2. **文档列表页** → `POST /api/v1/policy/documents/query`，随选择器状态传 `knowledge_bases`（与问答共用同一选择状态）
3. **用户提问** → `POST /chat` 携带当前 `knowledge_bases` 选择
4. **点击文档查看原文** → 用列表项的 `doc_id` 调 `GET /api/v1/policy/documents/{doc_id}/file`，拿 `Content-Type` + `Content-Disposition` 直接用浏览器内嵌预览（PDF 走浏览器查看器）或按 `X-Page-Count` 自行渲染

## 六、联调注意事项

- 选择器状态建议全局唯一份（问答与文档列表共享），避免两处选择不一致造成困惑
- `knowledge_bases` 传值必须与登录响应中的名字**逐字一致**（如"金科制度问答"），不要截断或改写
- 权限变更（管理员调整用户知识库权限）后，需用户重新登录才生效；若用户带着旧选择请求，后端会返回引导重新登录的提示（问答）或空列表（文档），前端收到此类响应时可提示刷新
