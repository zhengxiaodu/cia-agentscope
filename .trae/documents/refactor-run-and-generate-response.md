# 编排重构计划：orchestrator_service.run() 与 chat_service.generate_response()

## 摘要

对两个过长方法做**部分重构**：抽取辅助方法消除重复与巨型内联分支，保留核心编排主脉络内联不拆碎。重构后 `run()` 从 298 行降到约 90 行，`generate_response()` 从 244 行降到约 60 行，且消除 2 处 DRY 违规。不改动任何业务逻辑，只调整代码组织结构与注释。

## 现状分析

### orchestrator_service.py `run()` 方法（L465-762，298 行）
- 8 个逻辑阶段：取输入 → 装配组件 → 单 agent 分支 → 改写 → 识别 → 选编排器 → 加载状态 → 执行 → 保存
- 主脉络清晰（①~⑦ 编号注释），但有 3 个问题：
  1. **单 agent 分支内联 123 行**（L516-638）：与多 agent 主流程物理混杂，阅读须跳过整块
  2. **AgentState 加载/保存重复 2 次**：单 agent 路径（L536-549 加载 / L599-604 保存）与多 agent 路径（L715-731 加载 / L751-762 保存）逻辑相同
  3. **span 样板重复 4 次**：`start_span(...) if langfuse_service else _noop_ctx()` + `if span: try: span.update(...) except: pass`

### chat_service.py `generate_response()` 方法（L160-403，244 行）
- 8 个逻辑阶段：历史加载 → 快照 → 根span → 执行循环 → 推荐问题 → 持久化 → 文件检测 → trace 收尾
- 段落边界清楚，但有 2 个问题：
  1. **user_input 提取重复 2 次**（L257-269 推荐问题用 / L305-316 持久化用），且未复用已存在的 `OrchestratorService._extract_last_user_message`
  2. 持久化 / 文件检测 / trace 收尾段落混在 `with root_obs` 内，主脉络被收尾代码淹没

## 设计原则

- **保留主脉络内联**：run() 的改写→识别→选编排器→执行、generate_response() 的执行循环，是适配层核心，抽出去会割裂连贯性，得不偿失
- **只抽辅助与重复**：单 agent 分支、状态加载/保存、span 样板、历史加载、推荐问题、持久化、文件检测、trace 收尾
- **不改业务逻辑**：所有 SSE 事件、span 嵌套、yield 顺序、降级兜底行为保持完全一致
- **注释清新化**：每个抽取的方法有清晰 docstring；run()/generate_response() 内用编号注释标注主脉络阶段

## 具体改动

### 一、orchestrator_service.py

#### 新增私有方法（5 个）

1. **`_run_single_agent_path(...)` → AsyncGenerator[str, None]**
   - 职责：单 agent 直接问答全流程（校验→加载状态→创建 agent→span 内流式执行→收集输出→存状态→记标志→yield summary）
   - 来源：现 L516-638 整块
   - 参数：`registry, agent_factory, agent_id, user_input, session_id, user_id, session_service, langfuse_service, intent`
   - run() 中变为：`if agent_id: async for ev in self._run_single_agent_path(...): yield ev; return`

2. **`_load_agent_state(self, session_service, session_id, agent_id) -> Optional[AgentState]`**
   - 职责：加载单个 agent 状态 + trim 截断 + 异常兜底
   - 来源：现 L536-549（单 agent）与 L715-731（多 agent 循环体内）统一为调用此方法
   - 消除重复：单 agent 路径与多 agent 循环都调它

3. **`_persist_agent_state(self, session_service, session_id, user_id, agent_id, state_dict)`**
   - 职责：保存单个 agent 状态（带 session_service/非空校验）
   - 来源：现 L599-604（单 agent）与 L751-762（多 agent 循环体内）统一为调用此方法

4. **`_span(self, langfuse_service, name, input_dict)` → context manager**
   - 职责：统一 span 创建样板（`start_span if langfuse_service else _noop_ctx()`）
   - 来源：现 4 处重复的 `xxx_ctx = (langfuse_service.start_span(...) if langfuse_service else _noop_ctx())`
   - 替代：`with self._span(langfuse_service, "query-rewrite", {...}) as rw_span:`

5. **`_safe_update_span(span, output_dict)`**
   - 职责：安全更新 span 输出（`if span: try: span.update(...) except: pass`）
   - 来源：现 3 处重复的 span 收尾样板
   - 模块级函数（不依赖 self，放模块顶部 `_noop_ctx` 旁）

#### 重构后 run() 主脉络（约 90 行）

```python
async def run(self, messages, session_id, user_id, session_service, agent_id, request, search_enabled, langfuse_service):
    """编排主流程：取输入 → 装配组件 → 单agent短路 → 改写 → 识别 → 选编排器 → 加载状态 → 执行 → 保存。"""
    # ① 提取用户输入与历史
    user_input = self._extract_last_user_message(messages)
    history = self._extract_history(messages)
    if not user_input:
        yield self._event({"type": "error", "message": "未收到用户输入"}); return

    # ② 装配请求级组件（含 workspace 埋点）
    redis_client = getattr(request.app.state, "redis_client", None) if request else None
    registry, agent_factory, rewriter, recognizer = await self._build_request_components(...)

    # ③ 单 agent 短路路径
    if agent_id:
        async for ev in self._run_single_agent_path(registry, agent_factory, agent_id, user_input, ...):
            yield ev
        return

    # ④ 查询改写
    with self._span(langfuse_service, "query-rewrite", {...}) as rw_span:
        rewritten = await self._safe_rewrite(rewriter, user_input, history)
        self._safe_update_span(rw_span, {"rewritten": rewritten})
    yield self._event({"type": "query_rewritten", ...})

    # ⑤ 意图识别
    with self._span(langfuse_service, "intent-recognition", {...}) as ir_span:
        intent_result = await self._safe_recognize(recognizer, rewritten, history)
        self._safe_update_span(ir_span, {...})
    yield self._event({"type": "intents_recognized", ...})

    # ⑥ 选择编排器并加载各 agent 状态
    mode = recognizer.get_orchestration_mode(intent_result)
    orchestrator = self._create_orchestrator(mode, agent_factory)
    agent_states = {intent.agent: await self._load_agent_state(session_service, session_id, intent.agent)
                    for intent in intent_result.intents}

    # ⑦ 执行编排（透传 SSE 事件）
    self._last_agent_ids, self._last_success = [], True
    async for event_str in orchestrator.run(intent_result, session_id=session_id, agent_states=agent_states, langfuse_service=langfuse_service):
        yield event_str

    # ⑧ 保存各 agent 状态 + 记录编排结果
    self._last_orchestrator = orchestrator
    self._last_agent_ids = list(dict.fromkeys(r.agent_id for r in orchestrator._last_results if r.agent_id))
    for r in orchestrator._last_results:
        if r.final_state:
            await self._persist_agent_state(session_service, session_id, user_id, r.agent_id, r.final_state)
```

（注：`_safe_rewrite`/`_safe_recognize` 为内联 try/except 降级的小封装，也可不抽直接内联保留 try/except——实现时按可读性决定，倾向内联以减少方法数）

### 二、chat_service.py

#### 新增模块级/私有方法（5 个）

1. **`_load_history_messages(session_service, session_id) -> List[dict]`**
   - 职责：加载历史 + `[-6:]` 截断 + 异常兜底
   - 来源：现 L179-195

2. **`_extract_user_input(messages) -> str`**（模块级函数）
   - 职责：提取最后一条 user 消息文本（list/str 分支）
   - 来源：现 L257-269 与 L305-316 两处重复，统一为调用此函数
   - **消除 DRY 违规的关键**

3. **`_emit_recommended_questions(orchestrator_service, messages, final_output, langfuse_service) -> AsyncGenerator[str, None]`**
   - 职责：推荐问题生成（提取 user_input + span + 调用 `_generate_recommended_questions` + yield 事件 + 异常兜底）
   - 来源：现 L255-290

4. **`_persist_conversation_history(orchestrator_service, session_service, session_id, user_id, messages, final_output)`**
   - 职责：持久化历史（复用 `_extract_user_input` + 取 last_agent_ids/last_success + append_messages + 异常兜底）
   - 来源：现 L292-354

5. **`_detect_and_emit_files(session_id, before_files, session_service) -> AsyncGenerator[str, None]`**
   - 职责：文件快照 diff + yield files_generated + 持久化文件元信息
   - 来源：现 L356-376

6. **`_finalize_trace(langfuse_service, root_obs, session_service, session_id) -> AsyncGenerator[str, None]`**
   - 职责：flush + yield trace_ready + 存 trace_id（在 with root_obs 外执行）
   - 来源：现 L389-403

#### 重构后 generate_response() 主脉络（约 60 行）

```python
async def generate_response(orchestrator_service, messages, session_id, user_id, session_service, agent_id, request, search_enabled, langfuse_service):
    """对话主流程：加载历史 → 快照 → 根span → 执行编排 → 推荐问题 → 持久化 → 文件检测 → trace收尾。"""
    # ① 加载历史（限 3 轮）并合并当前输入
    history_messages = await _load_history_messages(session_service, session_id)
    full_messages = history_messages + messages
    final_output_parts: List[str] = []

    # ② 快照工作目录（用于结束后检测新文件）
    before_files = snapshot(os.path.join(WORKSPACE_BASEDIR, session_id)) if session_id else set()

    # ③ 根 span 包裹主体（保持活跃使子 span 自动嵌套）
    root_span_ctx = langfuse_service.start_span("chat-response", input={...}) if (langfuse_service and langfuse_service.enabled) else _noop_ctx()
    with root_span_ctx as root_obs:
        # ④ 执行编排主流程，实时透传 SSE 事件
        async for event_str in orchestrator_service.run(full_messages, ..., langfuse_service=langfuse_service):
            yield event_str
            # 解析 summary 收集输出 + 检测 CUSTOM_COMPONENT
            _process_event(event_str, final_output_parts)  # 内含 yield CUSTOM_COMPONENT

        final_output = "\n".join(final_output_parts).strip()

        # ⑤ 编排流结束立即生成推荐问题
        async for ev in _emit_recommended_questions(orchestrator_service, messages, final_output, langfuse_service):
            yield ev

        # ⑥ 持久化对话历史
        await _persist_conversation_history(orchestrator_service, session_service, session_id, user_id, messages, final_output)

        # ⑦ 检测本轮新文件
        async for ev in _detect_and_emit_files(session_id, before_files, session_service):
            yield ev

        # 更新根 span 输出（context manager 退出时自动 end）
        _safe_update_span(root_obs, {"reply": final_output, "session_id": session_id, "user_id": user_id})

    # ⑧ trace 收尾（with 外，保证 span 已 end）
    async for ev in _finalize_trace(langfuse_service, root_obs, session_service, session_id):
        yield ev
```

（注：`_process_event` 为内部辅助，解析 SSE 事件并填充 final_output_parts + yield CUSTOM_COMPONENT；若逻辑较简单也可保持内联）

## 不改动的部分

- 核心编排主脉络（改写→识别→选→执行）保持内联，不抽取
- generate_response() 的执行循环保持内联
- `with root_obs` 整体结构保留（保证 span 生命周期覆盖全流程）
- 所有业务逻辑：SSE 事件格式、span 嵌套层级、yield 顺序、降级兜底、`[-6:]` 截断、token 估算、success 判定、trace_id 持久化——全部不变

## 假设与决策

1. **决策：部分重构而非全面拆分**。探索确认两个方法"长但不乱"，全面拆分核心流程会割裂连贯性。
2. **决策：span helper 抽取为模块级 `_safe_update_span` + 实例方法 `_span`**。`_span` 依赖 `langfuse_service` 参数传入（非 self 状态），但放实例方法更符合 OO 风格；`_safe_update_span` 不依赖 self，放模块级。
3. **决策：`_safe_rewrite`/`_safe_recognize` 倾向不抽**，保留 try/except 内联在 with 块内，避免方法数膨胀。实现时若内联后 with 块仍过长再抽。
4. **假设：`_extract_last_user_message` 已是 OrchestratorService 静态方法**（探索确认存在），chat_service.py 的 `_extract_user_input` 独立实现即可，不强依赖跨类调用。
5. **假设：重构后 AST 语法校验 + 手工对照 yield 顺序**，确保事件流时序与重构前完全一致。

## 验证步骤

1. **AST 语法校验**：`python3 -c "import ast; ast.parse(open(f).read())"` 对两个文件
2. **yield 顺序对照**：重构前后逐个 SSE 事件类型（orchestration_start / query_rewritten / intents_recognized / ReplyStart/Agent 事件 / summary / recommended_questions / files_generated / trace_ready）的产出顺序必须一致
3. **span 嵌套对照**：重构前后 Langfuse trace 的 span 层级结构必须一致（chat-response 根 → workspace-load/query-rewrite/intent-recognition/agent-*/recommended-questions 子）
4. **单 agent 路径对照**：重构后 `_run_single_agent_path` 产出的事件序列与原内联代码一致（orchestration_start → error 或 ReplyStart/Agent 事件 → summary）
5. **降级路径对照**：改写失败降级、识别失败降级、span 未启用降级（`_noop_ctx`）行为一致
6. **git diff 审阅**：确认无业务逻辑变更，仅代码组织调整
7. **提交推送**：commit + push 到 `origin/trae/agent-5CYjia`
