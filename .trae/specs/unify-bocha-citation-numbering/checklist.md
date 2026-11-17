# Checklist

## 工具层（tools/bocha_search_tools.py）

- [x] 同一工具实例连续搜索编号连续递增，重复 URL 沿用原编号且保留首次登记信息（代码核验 + 用例 test_same_instance_shares_continuous_numbering / test_duplicate_url_reuses_id_and_first_registration）
- [x] 失败、空结果不占用编号；失败后重试编号从当前最大值连续递增（代码核验 + 用例 test_failure_does_not_consume_citation_ids）
- [x] 空/缺 URL 条目不进来源表、不出现在正文与 metadata；全部无效时返回"未搜索到相关结果。"且 metadata 为空（代码核验 + 用例 test_blank_or_missing_urls_filtered_partial / test_all_invalid_urls_returns_no_result）
- [x] metadata 快照为复制副本（列表 + 逐字典复制），后续工具调用不改变已返回快照（代码核验 + 用例 test_earlier_snapshot_immutable_after_new_registration）
- [x] 来源表为请求级闭包状态，代码注释固化"禁止全局/单例"与"条目登记后不可变"两条约束（模块 docstring 第 13-27 行 + 工厂 docstring 第 131-137 行）
- [x] URL 仅 strip 后精确比较，无尾斜杠/参数/scheme 归一化（代码核验 + docstring 明示）
- [x] _format_search_results 读取 citation_id，不再 enumerate(start=1)（代码核验第 84 行 + 用例用非连续 citation_id 2、5 验证编号取自字段）
- [x] 尾注指令包含六项约束：{n} 标注、不罗列链接、直接用所示编号、多搜索同一套编号、不自行重排、不沿用历史轮次编号（代码核验第 90-94 行 + 测试断言）
- [x] merge_bocha_snapshots 按 citation_id 幂等合并、升序输出、缺 citation_id 条目尾部保留（代码核验第 98-122 行 + 6 个专项用例）

## tracer 层（app/utils/agent_event_tracer.py）

- [x] 无 URL 去重逻辑，原样 extend 收集（代码核验第 91-95 行 + 用例 test_bocha_sum_same_url_not_deduplicated_across_events）
- [x] consume_bocha_sum 消费后清空语义保留（代码核验，16 个既有用例全部通过）

## 编排层（app/orchestrator/base.py、app/services/orchestrator_service.py）

- [x] bocha_emitted 无直接 extend，统一走 merge_bocha_snapshots（循环内与流末 residual 两处，两文件共 4 处，代码核验 + 全局检索无残留 extend）
- [x] 事件仍为全量快照语义（yield 事件结构未改动）

## 统一出口（app/services/chat_service.py）

- [x] bocha_sum 事件先合并再转发，不再原样透传（代码核验第 714-740 行拦截分支）
- [x] 仅出现新 citation_id 时发送完整快照；无变化快照与非搜索工具不触发发送（长度比较门控，第 721-738 行；非搜索工具无 metadata → tracer consume 为空 → 上游不发）
- [x] 原最后事件覆盖逻辑已删除，落库（正常+中断两路径）使用统一总表（自查 grep 确认无残留旧收集块）
- [x] 合并后列表按编号升序且编号连续无跳号，数组下标+1 == citation_id（连续分配 + 升序排序，由构造保证；merge 助手测试覆盖升序输出）

## 提示词（app/orchestrator/parallel.py）

- [x] _SUMMARY_SYSTEM_PROMPT 含"保留引用编号及其与语句对应关系"条目（第 37-39 行第 6 条）

## 测试与回归

- [x] tests/test_bocha_search_tools.py 新旧用例全部通过（28 个：适配 2 个既有 + 新增 13 个，含合并助手 6 个专项）
- [x] tests/test_agent_event_tracer.py 新旧用例全部通过（17 个：16 既有 + 1 新增不去重用例）
- [x] pytest 全量回归通过判定（577 用例：558 通过、19 失败均为历史遗留且经 git 核验与本次改造无关——mng AUTH_MOCK 短路 9 个、收藏路由历史拆分 7 个、DAO SQL 断言漂移 3 个；本次改造相关的测试面全部通过：直接用例 45 个 + chat_service/orchestrator 集成用例无失败）
- [x] 收藏链路 message_favorite_dao 为 INSERT…SELECT 原样复制，无需改动、自动兼容（人工核对：messages.bocha_sum 原样复制至 message_favorites，无格式耦合）
