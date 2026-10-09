# Tasks

- [x] Task 1: 新增 knowledge\_gaps 表与 DAO
  - [x] 1.1 `app/dao/init_mysql.py` 增加 `knowledge_gaps` 表 DDL（gap\_id/kb\_id/question/question\_hash/question\_type/status/empty\_count/时间戳，唯一键 uq\_gaps\_gap\_id，索引 kb\_id/question\_hash/status），幂等建表

  - [x] 1.2 新建 `app/regulations/dao/knowledge_gap_dao.py`：aiomysql 风格实现 upsert\_open\_gap（存在 open 同哈希则 empty\_count+1 并返回是否新建）、update\_question\_type、list\_open\_gaps\_grouped（按 kb\_id+question\_type 分组求和、按计数降序）、resolve\_gaps（按 gap\_ids 或 kb\_id 批量置 resolved）

- [x] Task 2: 模块基础层（config / schemas / providers）
  - [x] 2.1 新建 `app/regulations/config.py`：从环境变量读取 KB\_API\_BASE\_URL、KB\_INTERNAL\_TOKEN、TOP\_K\_OVERRIDE（默认 20）、RERANK\_TOP\_K\_OVERRIDE（默认 5）、query 理解各开关（总开关默认 True，business\_intents 默认 False）、REGULATIONS\_LANGFUSE\_PUBLIC\_KEY/SECRET\_KEY、REGULATIONS\_DASHBOARD\_USE\_REAL\_DATA（默认 false）、REGULATIONS\_DASHBOARD\_LOOKBACK\_DAYS（默认 30）；提供 `resolve_model(model_config)`：读取 model\_config.yml 的 models.regulations\_qa，缺失/关键字段为空时回退 models.default，返回统一 dict

  - [x] 2.2 新建 `app/regulations/schemas.py`：移植 GeneralQARequest、GeneralQAResponse、RetrieverResource 及大盘 Dashboard 系列 schema（从 qa\_app/schemas/chat.py 与 dashboard.py 合并精简，去掉会话类 schema）

  - [x] 2.3 新建 `app/regulations/providers/trace.py`：contextvar serial（new\_serial/set\_serial/get\_serial，组件前缀 oiapolyqa）

  - [x] 2.4 新建 `app/regulations/providers/langfuse.py`：移植 LangfuseClient（start\_trace/start\_span/score/_langfuse\_id/采样/propagate\_attributes），host 用主项目 LANGFUSE\_HOST，密钥用 REGULATIONS\_LANGFUSE_\*，未配置时全部 no-op

  - [x] 2.5 新建 `app/regulations/providers/llm.py`：移植 HttpxChatOpenAI（httpx 流式/非流式、reasoning\_content 捕获、app\_serial\_number 头、瞬时错误重试）、detect\_thinking\_mode/resolve\_thinking/build\_llm/get\_rewrite\_llm；模型参数来源改为注入的统一 model dict（不再读 qa\_app Settings）

- [x] Task 3: 检索与生成核心
  - [x] 3.1 新建 `app/regulations/services/kb_client.py`：移植 KBClient（/api/v1/retrieval、重试、字段映射、retrieve\_multi 并发+合并），serial 头与 retrieval span 埋点对接新 providers

  - [x] 3.2 新建 `app/regulations/services/retrieval_merge.py`、`citation_service.py`、`langfuse_meta.py`：原样移植（loguru→logging）

  - [x] 3.3 新建 `app/regulations/services/query_understanding/`：移植 business\_intents.py、entity\_config.json、memory.py、query\_understand.py（loguru→logging）

  - [x] 3.4 新建 `app/regulations/graph/`：移植 state.py、prompts.py、qa\_graph.py（understand/clarify/refuse/retrieve/generate 五节点与条件路由）

- [x] Task 4: 问答服务与知识缺口
  - [x] 4.1 新建 `app/regulations/services/policy_qa_service.py`：从 chat\_service.py 抽取 run\_general\_qa（blocking）与 run\_general\_qa\_stream（SSE，含 \_map\_event\_to\_sse、心跳、补发最终答案、错误分类），去掉会话/标题/建议相关逻辑；持有 graph、kb\_client、model 配置

  - [x] 4.2 新建 `app/regulations/services/knowledge_gap_service.py`：record\_gap（新缺口时后台 LLM 概括 question\_type）、load\_open\_gaps\_grouped、resolve\_gaps；DB 操作改用 KnowledgeGapDAO（mysql\_pool），LLM 用 providers/llm

- [x] Task 5: 数据大盘
  - [x] 5.1 新建 `app/regulations/services/langfuse_query.py` 与 `kb_dashboard_client.py`：移植（Langfuse 查询用制度问答密钥）

  - [x] 5.2 新建 `app/regulations/services/dashboard_aggregation.py` 与 `dashboard_service.py`：移植聚合口径（overview/trend/usage/top\_cited、is\_refused 过滤、P95 口径）、Mock 数据、300s 缓存与失效、空数据兜底

- [x] Task 6: 路由与主项目接线
  - [x] 6.1 新建 `app/routes/policy_qa.py`：POST /api/v1/policy/qa，get\_current\_user 依赖，按 response\_mode 分发 blocking/streaming

  - [x] 6.2 新建 `app/routes/dashboard.py`：GET /dashboard/kb-overview、POST /dashboard/knowledge-gaps/resolve，get\_current\_user 依赖（去掉内部 token）

  - [x] 6.3 修改 `app/main.py`：lifespan 中解析 model\_config 后初始化 PolicyQAService（含 KBClient、mysql\_pool 注入）挂 app.state；注册两个新路由

- [x] Task 7: policy\_qa 工具本地化
  - [x] 7.1 修改 `tools/policy_qa_tools.py`：删除 httpx 远程调用与 POLICY\_QA\_BASE\_URL，改为进程内调用 PolicyQAService.run\_general\_qa（透传 user\_id/department）；保持权限映射、引用格式化、ToolChunk metadata 契约不变

  - [x] 7.2 删除 `app/config.py` 与 `.env.example` 中 POLICY\_QA\_BASE\_URL；.env.example 新增 REGULATIONS\_\* 配置段（Langfuse 密钥、KB API、大盘开关）与说明

- [x] Task 8: 模型与依赖配置
  - [x] 8.1 `config/model_config.yml` 新增 models.regulations\_qa 条目（字段留空，注释说明为空回退 default）

  - [x] 8.2 `requirements.txt` 新增 langgraph、langchain-openai、langchain-core

- [x] Task 9: 测试
  - [x] 9.1 移植适配单元测试：policy\_qa\_service（blocking/streaming 事件序列、空应答缺口触发）、kb\_client（重试/字段映射/retrieve\_multi 合并）、knowledge\_gap\_dao/service（upsert/分组/resolve）、dashboard（Mock 结构、聚合口径、缓存失效）、config 回退逻辑

  - [x] 9.2 tools/policy_qa_tools 本地调用测试（权限映射、无权限、服务异常兜底文案）
  - [x] 9.3 全量回归：现有 pytest 套件通过

# Task Dependencies

- Task 1、2 可并行；Task 3 依赖 2；Task 4 依赖 1、3；Task 5 依赖 2（与 3/4 可并行开发 aggregation 部分，联调依赖 4.2 的缺口读取）；Task 6 依赖 4、5；Task 7 依赖 6（需要 app.state 服务就绪）；Task 8 与 1-7 并行；Task 9 依赖 7

