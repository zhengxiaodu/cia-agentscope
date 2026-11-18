# 博查搜索来源统一编号（citation_id）Spec

## Why

同一轮内多次搜索、多智能体并发搜索时，每次工具调用各自 `enumerate(start=1)` 编号，导致：同轮不同搜索的 `[n]` 编号互相冲突、正文 `{n}` 与最终来源列表错位、重复 URL 重复展示。需把"URL 去重 + 编号分配"收敛到工具一处，下游只做按 `citation_id` 的合并，SSE 展示与落库使用同一份来源表。

## What Changes

- `tools/bocha_search_tools.py`：闭包维护请求级来源表（来源列表 + URL→citation_id 映射）；搜索成功后先过滤、去重、分配连续编号，再生成工具文本与 metadata 全量快照；新增空/缺 URL 过滤规则；新增共享合并助手 `merge_bocha_snapshots`。
- `app/utils/agent_event_tracer.py`：删除 tracer 内独立 URL 去重，改为纯收集透传，保留消费后清空语义。
- `app/orchestrator/base.py`、`app/services/orchestrator_service.py`：`bocha_emitted` 由直接 `extend` 改为按 `citation_id` 合并 + 按编号排序。
- `app/services/chat_service.py`：统一出口按 `citation_id` 合并到本轮总表、排序，仅在出现新编号时发送完整快照；落库直接使用该总表（替换"取最后事件覆盖"逻辑）。
- `app/orchestrator/parallel.py`：多智能体汇总系统提示词增加"保留原有引用编号及其与语句对应关系"。
- 工具返回文本尾注引用指令更新：直接使用所示编号、多次搜索沿用同一套编号、不自行重排、不沿用历史轮次编号。
- 测试：`tests/test_bocha_search_tools.py`、`tests/test_agent_event_tracer.py` 适配并新增编号相关用例。
- **前端不改代码**：兼容性由后端保证——合并后列表按编号升序、编号连续无跳号，故"数组下标+1 == citation_id"对旧渲染逻辑成立；新消息含 `citation_id` 供前端优先映射（前端侧约定，另行同步）。

## Impact

- Affected code:
  - `tools/bocha_search_tools.py`（核心改动）
  - `app/utils/agent_event_tracer.py`
  - `app/orchestrator/base.py`
  - `app/services/orchestrator_service.py`
  - `app/services/chat_service.py`
  - `app/orchestrator/parallel.py`
  - `tests/test_bocha_search_tools.py`、`tests/test_agent_event_tracer.py`
- 兼容性：
  - 旧消息 `bocha_sum` 无 `citation_id`，前端用数组下标+1 兜底渲染，不批量修改历史数据。
  - 收藏链路（`message_favorite_dao` 的 INSERT…SELECT 原样复制 `messages.bocha_sum`）自动兼容，无需改动。
- 非目标（明确不做）：
  - 前端代码修改（仅约定兜底规则）。
  - Redis/跨实例状态（当前每请求创建工具实例，负载均衡下天然隔离）。
  - URL 归一化（不做尾斜杠/参数/http-https 合并，避免误合并不同页面；代价仅是近重复 URL 各占一个编号，属展示冗余）。
  - 跨轮旧编号误用的机制性消除（提示词仅降概率，属已知边界）。

## ADDED Requirements

### Requirement: 请求级统一来源编号

系统 SHALL 在 `create_bocha_search_tool()` 闭包内维护本轮请求唯一的来源表（来源条目列表 + URL→citation_id 映射），同轮所有智能体共享；工具实例每请求新建，下一轮编号从 1 重新开始。SHALL NOT 将来源表置于模块全局变量或服务单例中。

登记规则：
- URL 仅做首尾空白 strip 后精确比较，不做其他归一化。
- 新 URL 分配下一个连续编号（从 1 开始）；重复 URL 沿用原编号。
- 来源条目首次登记后字段不可变（重复 URL 保留首次登记的信息，不更新）。
- 搜索失败、无结果不占用编号。
- 编号连续无跳号（1..N），使排序后"数组下标+1 == citation_id"成立。

#### Scenario: 同轮多次搜索连续编号
- **WHEN** 同一工具实例依次执行两次搜索，第一次返回来源 A、B，第二次返回来源 B、C
- **THEN** 第一次文本展示 `[1] A`、`[2] B`；第二次文本展示 `[2] B`、`[3] C`；两次 metadata 快照分别为 [A(1),B(2)] 与 [A(1),B(2),C(3)]

#### Scenario: 重复 URL 沿用编号且首次登记不可变
- **WHEN** 某 URL 再次出现且其摘要字段与首次登记不同
- **THEN** 沿用首次分配的 citation_id，来源表保留首次登记的字段值

#### Scenario: 空/缺 URL 过滤
- **WHEN** 搜索返回中存在 url 缺失或 strip 后为空白的条目
- **THEN** 该条目不进来源表、不分配编号、不出现在工具正文与 metadata 快照中
- **WHEN** 过滤后无任何有效条目
- **THEN** 返回"未搜索到相关结果。"，metadata 为空，不占编号

#### Scenario: 失败/空结果不占编号
- **WHEN** 请求超时、网络异常、HTTP/业务错误、响应无结果
- **THEN** 返回原有友好文案，metadata 为空，来源表与编号不受影响；随后重试成功的编号从当前最大值连续递增

#### Scenario: 快照不可变
- **WHEN** 工具返回 metadata 后，同一实例继续执行新搜索
- **THEN** 先前返回的 metadata 内容不变（快照为列表+字典的复制副本，字段均为不可变字符串，无递归深拷贝）

#### Scenario: 请求隔离
- **WHEN** 调用 `create_bocha_search_tool()` 创建新实例
- **THEN** 来源表为空、编号从 1 开始，与之前任何实例无共享状态

### Requirement: 按 citation_id 的统一合并出口

系统 SHALL 提供共享助手 `merge_bocha_snapshots(existing, incoming)`（位于 `tools/bocha_search_tools.py`）：按 `citation_id` 幂等合并去重、按编号升序输出；缺失 `citation_id` 的条目原样保留在输出尾部（防御性，正常流中不出现）。`app/orchestrator/base.py`、`app/services/orchestrator_service.py` 与 `app/services/chat_service.py` SHALL 复用该助手。

`chat_service.py` SHALL 作为统一出口：
- `bocha_sum` 事件不再原样透传，先合入本轮总表；
- 仅当出现新 citation_id 时重建并发送完整快照事件，否则丢弃该事件；
- 最终落库（正常路径与中断路径）直接使用该总表。

保证范围（准确表述）：统一出口消除的是**展示与落库走不同合并逻辑**导致的不一致（后端发送与准备落库使用同一张表）；网络断连、落库失败、前端自身处理问题不在此方案范围内。

#### Scenario: 乱序快照不回退
- **WHEN** 多智能体并发搜索、各自 tracer 的全量快照交错到达，较旧快照（编号少）晚于较新快照（编号多）到达
- **THEN** 合并后总表不丢来源、不重复、编号不回退；较旧快照因无新编号被门控丢弃

#### Scenario: 无变化与非搜索工具不重发
- **WHEN** 收到的快照不含新编号（全部为已合并来源），或非搜索工具结束时
- **THEN** 不发送任何 bocha_sum 事件

#### Scenario: SSE 与落库一致
- **WHEN** 本轮结束时查看最后发送的完整快照与落库的 `bocha_sum` 字段
- **THEN** 二者为同一张按编号升序的总表

### Requirement: 引用指令与汇总提示词约束

工具返回文本尾注 SHALL 包含完整编号规则：正文用 `{n}` 标注引用、不罗列完整链接、直接使用所示编号、多次搜索沿用同一套编号、不自行重排、不沿用历史轮次编号。

`app/orchestrator/parallel.py` 的 `_SUMMARY_SYSTEM_PROMPT` SHALL 增加条目：保留各任务结果中已有引用编号及其与语句的对应关系，不改动编号。

#### Scenario: 工具尾注含编号规则
- **WHEN** 搜索成功返回结果文本
- **THEN** 末尾指令包含上述六项约束

#### Scenario: 多智能体汇总保留编号
- **WHEN** 并行编排触发 LLM 汇总多个子任务结果
- **THEN** 汇总系统提示词要求保留引用编号及对应关系

## MODIFIED Requirements

### Requirement: 工具返回文本编号来源

原实现：`_format_search_results` 对每次搜索结果 `enumerate(start=1)` 编号，每次从 1 开始。

新实现：编号读取条目的 `citation_id` 字段（由来源表分配），取消每次从 1 开始的 enumerate；文本仅展示本次搜索的有效条目及其全局编号。

### Requirement: tracer 的 bocha_sum 收集语义

原实现：`_collect_bocha_sum` 内按 URL 去重（保序）后累积。

新实现：不做任何去重，`extend` 原样收集已编号条目（去重职责收敛到工具）；保留"消费后清空"语义，避免非搜索工具结束时反复输出旧数据。

### Requirement: 编排层 bocha_emitted 累积方式

原实现：`bocha_emitted.extend(consumed)` 直接追加，配合 tracer 去重防重复。

新实现：`bocha_emitted = merge_bocha_snapshots(bocha_emitted, consumed)`（循环内与流末 residual 两处），事件仍发全量快照。

### Requirement: chat_service 落库来源提取

原实现：`collected_bocha_sum = payload.get("bocha_sum") or []`（取最后事件覆盖）。

新实现：按 citation_id 合并到本轮总表，落库使用总表；事件转发受变更门控（见统一合并出口）。

### Requirement: 前端来源映射兼容

原兼容方式：前端按数组下标映射 `{n}`。

新兼容方式：新消息每条来源含 `citation_id`，前端优先按其映射；旧消息（无该字段）用数组下标+1 兜底。该兜底成立依赖四个条件同时满足：完整快照、连续分配、失败不占号、不删来源且按编号排序——后端代码 SHALL 保持这些不变量（其中"来源条目登记后不可变"为变更门控的前提，SHALL 在代码注释中固化）。

## REMOVED Requirements

### Requirement: tracer 内 URL 去重
**Reason**: 去重与编号职责收敛到工具的来源表，tracer 层去重与工具层映射键语义重复且无编号信息。
**Migration**: 无需迁移——工具输出的快照已按 URL 去重并携带编号，下游统一按 citation_id 幂等合并。

## 已知边界（接受，不扩大改造）

1. **跨轮旧编号误用**：agent 上下文保留近 3 轮历史，模型可能沿用上轮 `{n}` 指向本轮其他来源，属真实引用错位风险；本方案不解决，仅靠尾注与提示词降低概率。
2. **URL 近重复**：同页面以尾斜杠/参数/scheme 差异重复出现时会各占一个编号，代价仅是来源表展示冗余，不产生对应关系错误。
3. **变更检测维度**：来源条目登记后不可变、编号只增不减，故"出现新 citation_id"是唯一变化维度，长度比较即可判定变更；若未来引入"重复来源更新最新摘要"等逻辑将静默破坏门控，须同步重审。
