# 接入安全敏感内容检测服务 Spec

## Why
当前系统对用户输入与智能体输出无任何内容安全审核，存在敏感词（政治、暴力、色情等）直接进入编排或直接返回前端的风险。需要接入统一的敏感检测服务（本地词典 + MiniCPM5 语义模型），在两个关键节点做拦截：用户输入（创建工作区之前）与编排流结束后的 `final_output`。命中敏感时通过 `message_replace` 事件流告知用户原因并停止后续输出；服务异常时兜底放行，确保主流程不受影响；每次调用都要记录到 Langfuse。

## What Changes
- `.env` 新增安全敏感服务地址、阈值、超时配置
- `app/config.py` 新增 `SENSITIVE_SERVICE_URL`、`SENSITIVE_THRESHOLD`、`SENSITIVE_TIMEOUT` 配置项
- 新建 `app/services/sensitive_service.py`：封装敏感检测 HTTP 调用，含超时控制、异常兜底、Langfuse 埋点
- `app/routes/chat.py`：在 `/chat` 流式响应中、调用 `generate_response` 之前，对用户输入做敏感检测；命中则 yield `message_replace` 事件并跳过主流程
- `app/services/chat_service.py`：在 `generate_response` 收集完 `final_output` 后、生成推荐问题之前，对 `final_output` 做敏感检测；命中则 yield `message_replace` 事件并跳过推荐问题（持久化与文件检测仍执行，保证数据完整）

## Impact
- Affected specs: 无（新增能力）
- Affected code:
  - `/workspace/.env`
  - `/workspace/app/config.py`
  - `/workspace/app/services/sensitive_service.py`（新增）
  - `/workspace/app/routes/chat.py`
  - `/workspace/app/services/chat_service.py`
- 兼容性：`SENSITIVE_SERVICE_URL` 为空时全部跳过检测，行为与现状一致；服务异常时兜底放行，不影响主流程

## ADDED Requirements

### Requirement: 用户输入敏感检测
系统 SHALL 在 `/chat` 接口创建工作区之前，提取用户最后一条消息的文本并调用敏感检测服务。命中敏感时，向客户端发送 `message_replace` 事件，并停止后续编排与流式输出。

#### Scenario: 用户输入命中敏感词
- **WHEN** 用户提交包含敏感内容的提问
- **THEN** 系统调用敏感检测服务返回 `hasSensitiveWord=true`
- **AND** 向客户端发送 `message_replace` 事件，`message` 字段提示"您的内容涉及敏感词/模型给出的风险原因，请修改提问"，`reason` 字段携带模型给出的风险原因
- **AND** 不进入 `generate_response`，不创建工作区，不产生编排

#### Scenario: 用户输入未命中敏感
- **WHEN** 用户提交正常提问
- **THEN** 敏感检测返回 `hasSensitiveWord=false`
- **AND** 正常进入 `generate_response` 主流程

#### Scenario: 服务地址未配置
- **WHEN** `SENSITIVE_SERVICE_URL` 为空
- **THEN** 跳过敏感检测，直接进入主流程

### Requirement: 编排输出敏感检测
系统 SHALL 在 `generate_response` 编排流结束、收集完 `final_output` 后、生成推荐问题之前，对 `final_output` 调用敏感检测服务。命中时向客户端发送 `message_replace` 事件，并跳过推荐问题生成；持久化与文件检测仍执行。

#### Scenario: final_output 命中敏感
- **WHEN** 编排输出包含敏感内容
- **THEN** 向客户端发送 `message_replace` 事件，提示内容涉及敏感词并附风险原因
- **AND** 跳过推荐问题生成
- **AND** 仍执行对话历史持久化与文件检测，保证会话数据完整

#### Scenario: final_output 未命中敏感
- **WHEN** 编排输出正常
- **THEN** 正常进入推荐问题生成等后续步骤

### Requirement: 异常兜底
系统 SHALL 在敏感检测服务出现任何异常（网络错误、超时、HTTP 非 2xx、JSON 解析失败）时，记录日志并放行，继续正常流程，不阻断用户请求。

#### Scenario: 服务超时或不可达
- **WHEN** 敏感检测服务在 `SENSITIVE_TIMEOUT` 内未响应或连接失败
- **THEN** 记录 warning 日志，视为未命中，继续主流程

#### Scenario: 服务返回业务错误码
- **WHEN** 响应 `code != 0`（如 50001 模型异常）
- **AND** 响应体可解析
- **THEN** 按服务文档判定：若 `hasSensitiveWord=true` 则按命中处理；若 `hasSensitiveWord` 字段缺失或不可信，记录日志并放行

### Requirement: Langfuse 埋点
系统 SHALL 在每次敏感检测服务调用时，通过 Langfuse 记录一个 span，包含输入文本、输出响应、是否命中、耗时等元数据。

#### Scenario: 正常调用并记录
- **WHEN** 敏感检测服务被调用
- **THEN** 在 Langfuse 中创建名为 `sensitive-check` 的 span
- **AND** span 的 `input` 包含待检测文本与阈值
- **AND** span 的 `output` 包含服务原始响应、`blocked` 标志、风险分类与原因
- **AND** span 的 `metadata` 包含耗时（ms）、调用阶段（input/output）

### Requirement: 配置项
系统 SHALL 通过环境变量配置敏感检测服务地址与参数。

#### Scenario: 默认配置
- **WHEN** 未设置环境变量
- **THEN** `SENSITIVE_SERVICE_URL` 默认为空（关闭检测）
- **AND** `SENSITIVE_THRESHOLD` 默认 `0.7`
- **AND** `SENSITIVE_TIMEOUT` 默认 `5`（秒）

## message_replace 事件结构
```json
{
  "type": "message_replace",
  "message": "您的内容涉及敏感词，请修改提问",
  "reason": "模型给出的风险原因",
  "stage": "input | output"
}
```
- `stage`: `input` 表示用户输入检测命中，`output` 表示编排输出检测命中
- 前端收到后应替换当前消息内容并停止接收后续流

## 兜底策略
| 异常类型 | 处理 |
| --- | --- |
| `SENSITIVE_SERVICE_URL` 为空 | 跳过检测，放行 |
| 连接错误 / 超时 | 记录 warning，放行 |
| HTTP 非 2xx | 记录 warning，放行 |
| JSON 解析失败 | 记录 warning，放行 |
| `code=0` 且 `hasSensitiveWord=true` | 命中拦截 |
| `code=0` 且 `hasSensitiveWord=false` | 放行 |
| `code!=0`（业务异常） | 记录 warning，放行 |

## 敏感检测服务调用契约
- URL：`{SENSITIVE_SERVICE_URL}`（默认 `http://25.59.38.152:30017/sensitive/check`）
- Method：`POST`
- Content-Type：`application/json`
- Body：`{"text": "<待检测文本>", "threshold": 0.7}`
- 超时：`SENSITIVE_TIMEOUT` 秒
