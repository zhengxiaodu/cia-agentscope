# Tasks

- [x] Task 1: 改造 tools/bocha_search_tools.py：请求级来源表 + 编号 + 过滤 + 快照 + 合并助手
  - [x] 1.1 闭包内维护来源表（来源条目列表 + URL→citation_id 映射），代码注释固化两条约束：来源表生命周期=单次请求（禁止模块全局/单例/缓存复用工具实例）、来源条目首次登记后字段不可变（变更门控前提）
  - [x] 1.2 搜索成功后在同一不含 await 的同步步骤内完成：URL strip 判空过滤 → 新 URL 分配连续编号 / 重复 URL 沿用原编号且保留首次登记信息 → 复制快照（列表 + 逐字典复制，字段均为字符串，无递归深拷贝）
  - [x] 1.3 过滤后无有效条目时返回"未搜索到相关结果。"（metadata 为空，不占编号）；失败路径维持现状（不占编号）
  - [x] 1.4 _format_search_results 改为读取条目 citation_id 字段（删除 enumerate(start=1)），仅展示本次搜索的有效条目；尾注引用指令更新为六项约束：正文用 {n} 标注、不罗列完整链接、直接使用所示编号、多次搜索沿用同一套编号、不自行重排、不沿用历史轮次编号
  - [x] 1.5 新增模块级助手 merge_bocha_snapshots(existing, incoming) -> list：按 citation_id 幂等合并去重、按编号升序输出，缺失 citation_id 的条目原样保留在输出尾部；供编排层与 chat_service 复用
- [x] Task 2: 改造 app/utils/agent_event_tracer.py：移除 URL 去重
  - [x] 2.1 _collect_bocha_sum 中 bocha_sum 分支删除 seen_urls 去重块，改为直接 extend 原样收集；保留 consume_bocha_sum 消费后清空语义
- [x] Task 3: 改造 app/orchestrator/base.py 与 app/services/orchestrator_service.py：按 citation_id 合并
  - [x] 3.1 两处 bocha_emitted.extend(...)（循环内 consume 与流末 residual）改为 bocha_emitted = merge_bocha_snapshots(bocha_emitted, consumed)，事件仍发全量快照（从 tools.bocha_search_tools 导入助手）
- [x] Task 4: 改造 app/services/chat_service.py：统一出口合并与变更门控
  - [x] 4.1 事件转发分支新增 bocha_sum 拦截：不再原样透传，先 merge_bocha_snapshots 合入本轮总表（collected_bocha_sum）并排序
  - [x] 4.2 变更门控：仅当合并出现新 citation_id（总表长度增长）时重建并发送 {"type": "bocha_sum", "bocha_sum": 总表} 事件，否则丢弃该事件；长度比较的合法性依赖"编号只增不减"需注释说明
  - [x] 4.3 删除原 line ~745-748 的"取最后事件覆盖"收集逻辑（避免覆盖统一总表）；落库两处调用点（正常路径与中断 finally 路径）自动使用统一总表，无需改动
- [x] Task 5: 更新 app/orchestrator/parallel.py 汇总提示词
  - [x] 5.1 _SUMMARY_SYSTEM_PROMPT 追加条目：保留各任务结果中已有引用编号（如 {1}）及其与语句的对应关系，不改动编号
- [x] Task 6: 测试更新与新增
  - [x] 6.1 tests/test_bocha_search_tools.py：适配新格式（metadata 条目含 citation_id、_format_search_results 依赖 citation_id）；新增用例：同实例连续搜索编号连续递增、重复 URL 沿用编号且保留首次登记、空/缺 URL 过滤（部分有效/全部无效）、失败后重试不占编号、快照不可变（后续调用不改先前 metadata）、新实例从 1 开始、全量快照含先前来源、merge_bocha_snapshots（乱序不回退/重复不重复合并/按编号排序/缺 citation_id 尾部保留）
  - [x] 6.2 tests/test_agent_event_tracer.py：适配纯收集语义，新增"同 URL 两次到达不去重"用例；核对既有 consume 清空用例仍通过
- [x] Task 7: 全量验证
  - [x] 7.1 运行 pytest 全量（纯内存，不连外部服务），确认新旧用例全部通过；对照 checklist.md 逐项核对并勾选（577 用例 558 通过，19 个失败均为历史遗留、经 git 核验与本次改造无关）

# Task Dependencies

- Task 1 是基础（定义快照格式与合并助手），Task 2/3/4/5 均依赖 Task 1，四者之间无依赖可并行
- Task 6 依赖 Task 1-5 全部完成
- Task 7 依赖 Task 6
