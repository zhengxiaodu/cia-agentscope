# 会话消息配对与生成文件持久化 Spec

## Why
会话历史详情无法以"一轮问答"为单位组织内容：messages 表缺少配对标识，session_files 表不知道文件属于哪条消息，且文件只存在于会过期销毁的沙箱中，会话结束后无法下载；同时制度问答的 citations 引用信息与用户上传的文件也未在历史详情接口中返回。

## What Changes
- `messages` 表新增 `message_pair_id`（每轮对话生成一个随机 UUID，本轮 user 提问与 assistant 回答共享同值）与 `citations`（制度问答引用信息，JSON，可为空）两列；历史详情接口每条 message 带上这两个字段
- `session_files` 表新增 `message_id` / `message_pair_id`，明确每个生成文件对应哪条消息；历史详情接口 `files` 字段每项带上这两个字段
- 新增生成文件持久化：`.env` 新增持久化目录配置 `SESSION_FILES_PERSIST_DIR`；每轮持久化对话历史（messages 落库）之后检测新文件，读取新文件字节流（复用 `read_session_file` 通道）写入 `{持久化目录}/{session_id}/{rel_path}`，session_files 记录持久化下载 url；读取或写盘失败时回退记录沙箱 url
- 新增持久化文件下载接口 `GET /persist-files/{session_id}/{path}`，从持久化目录下载文件
- 历史详情接口新增 `upload_files` 字段（每个上传文件的名字/大小/类型/创建时间/message_id/message_pair_id）；`upload_files` 表新增 `message_pair_id`，问答结束绑定时与 `message_id` 一并回填
- 顺序确认（回答需求 3 末尾的问题）：**持久化对话历史保持在检测本轮新文件之前**，维持现状 ⑥→⑦ 顺序不变——因为 session_files 记录需要 messages 落库产生的 message_id / message_pair_id；而"读取新文件字节流→写持久化目录→写 session_files"发生在检测出新文件清单之后（⑦ 内部）

## Impact
- Affected specs: `persist-and-return-session-files`（session_files 记录内容扩展）、`multi-turn-session`（messages 表结构与历史详情响应扩展）
- Affected code:
  - `app/dao/init_mysql.py`（三张表加列 + ALTER 存量兼容）
  - `app/dao/mysql_session_dao.py`（append_messages / load_messages / append_session_files / load_session_files）
  - `app/dao/upload_file_dao.py`（bind_message_id 扩展、新增 list_files_by_session）
  - `app/models/session.py`（SessionMessage / SessionFile 扩展、新增 SessionUploadFile、SessionDetailResponse 扩展）
  - `app/services/session_service.py`（构造注入 upload_file_dao、get_session_detail 加载 upload_files）
  - `app/services/chat_service.py`（message_pair_id 生成、citations 捕获、文件字节流持久化、url 切换）
  - `app/routes/files.py`（新增 /persist-files 下载接口）
  - `app/config.py` / `.env` / `.env.example`（SESSION_FILES_PERSIST_DIR）
  - `app/main.py`（SessionService 接线调整）
- **BREAKING**（内部接口，无外部 API 变更）：`SessionDAO.append_messages` 返回值由 `Optional[int]`（user 消息 id）改为 `Optional[dict]`（`{"user_message_id": int|None, "assistant_message_id": int|None}`），调用方 `chat_service._persist_conversation_history` 与相关测试同步修改

## ADDED Requirements

### Requirement: 生成文件持久化
系统 SHALL 在每轮对话检测出新增文件后，将文件字节流从沙箱读取并写入 `SESSION_FILES_PERSIST_DIR/{session_id}/{rel_path}`（保留子目录结构），并将持久化下载 url（`/persist-files/{session_id}/{rel_path}`）连同 message_id / message_pair_id 记入 session_files 表。

#### Scenario: 持久化成功
- **WHEN** 本轮检测出新文件 `report/a.docx` 且已配置 SESSION_FILES_PERSIST_DIR
- **THEN** 字节流写入 `{SESSION_FILES_PERSIST_DIR}/{session_id}/report/a.docx`；session_files 该记录 `url=/persist-files/{session_id}/report/a.docx`，`message_id` = 本轮 assistant 消息 id，`message_pair_id` = 本轮配对 id

#### Scenario: 持久化失败回退
- **WHEN** 读取沙箱字节流失败、写盘失败，或 SESSION_FILES_PERSIST_DIR 未配置（留空）
- **THEN** 记 warning 不中断；session_files 照常落库，`url` 回退沙箱地址 `/files/{session_id}/{rel_path}`，message_id / message_pair_id 照常记录

### Requirement: 持久化文件下载接口
系统 SHALL 提供 `GET /persist-files/{session_id}/{path:path}`，登录用户可从持久化目录下载文件；鉴权、路径穿越防护与 mode=download/inline 语义与现有 `/files/{session_id}/{path}` 一致，复用既有 Content-Disposition 中文文件名编码方案。

#### Scenario: 下载成功
- **WHEN** 已登录用户请求存在的持久化文件
- **THEN** 返回文件字节流，media_type 按扩展名推断，attachment 头含 UTF-8 编码的原文件名

#### Scenario: 文件不存在 / 路径越权
- **WHEN** 持久化目录下无该文件，或 path 含 `..` / 绝对路径
- **THEN** 分别返回 404 / 403

### Requirement: 历史详情返回 upload_files
`GET /sessions/{session_id}` 响应 SHALL 新增 `upload_files` 字段，列出该会话全部上传文件（含未被对话消费的记录），每项含 name（文件名）、size（字节数）、media_type（类型）、created_at（创建时间，格式 `%Y-%m-%d %H:%M:%S.%f` 截断毫秒）、message_id（未消费为 null）、message_pair_id（未绑定为 null）。

#### Scenario: 会话含上传文件
- **WHEN** 会话内上传过 2 个文件，其中 1 个已被某轮问答消费
- **THEN** upload_files 返回 2 条记录，已消费记录带该轮 user 消息的 message_id 与 message_pair_id，未消费记录两者为 null

## MODIFIED Requirements

### Requirement: 消息持久化（messages 表）
每轮对话开始时生成 `message_pair_id`（`uuid.uuid4().hex`），本轮 user 消息与 assistant 消息以同值落库；assistant 消息可携带制度问答 citations（从编排流捕获 `policy_qa_citations` 事件累积，多智能体路径多条事件合并，无则为空不写）。`append_messages` 通过消息 dict 读取 `message_pair_id` / `citations` 字段写入对应列，返回值改为 `{"user_message_id", "assistant_message_id"}`。用户中断（finally 路径）同样携带 message_pair_id 与已捕获的 citations 落库。

### Requirement: 会话历史详情接口
`GET /sessions/{session_id}` 返回扩展为：
- `messages`：每条新增 `message_pair_id`（存量旧消息为 null）、`citations`（无引用为 `[]`）
- `files`：每条新增 `message_id`（生成文件对应本轮 assistant 消息 id）、`message_pair_id`
- `upload_files`：见 ADDED Requirement

### Requirement: 上传文件消息绑定
`UploadFileDAO.bind_message_id(session_id, message_id, message_pair_id)` 在问答结束回填时，将该会话全部未绑定上传文件的 `message_id` 与 `message_pair_id` 一并写入。

### Requirement: session_files 记录
`append_session_files` 写入/UPSERT 时携带 message_id、message_pair_id（ON DUPLICATE KEY UPDATE 同步更新这两列）；`load_session_files` 返回这两列。files_generated SSE 事件 payload 同步附带 message_id / message_pair_id（additive，不破坏既有契约）。

## REMOVED Requirements
（无）
