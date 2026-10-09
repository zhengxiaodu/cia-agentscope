# 意图识别/编排拆分 + 流式进度 + workspace 初始化埋点

## 摘要

将当前单次 LLM 意图识别拆成两次 LLM 调用：①意图识别（输出 intents 列表）②意图编排（输出 relation + 执行顺序）。两次调用各自独立 langfuse 子 span + 环节级进度 SSE 事件。另外单独记录首次 `ws.initialize()` 的耗时 span。

## 进度状态

- ✅ **Task 1** `intent/models.py`：已新增 `execution_order: List[int]` 字段（L43）
- ✅ **Task 2** `config/model_config.yml`：已改写 `intent_recognition` prompt（只输出 intents）+ 新增 `intent_orchestration` prompt（输出 relation+execution_order）
- ✅ **Task 3** `intent/recognizer.py`：已拆成 `recognize_intents()` + `plan_orchestration()` + `recognize()` 兼容入口，构造函数已新增 `orchestration_prompt` 参数
- ⏳ **Task 4** `orchestrator_service.py`：待执行（3 处改动，详见下文）
- ⏳ **Task 5** AST 校验 + git commit & push

> ⚠️ **当前代码已破损**：`_build_request_components()` L458-464 构造 `IntentRecognizer` 时未传 `orchestration_prompt`，而重构后的构造函数要求该参数，运行时会 `TypeError`。Task 4 必须修复。

## 现状分析（Task 4 相关）

### orchestrator_service.py
- **`_build_request_components()` L458-464**：构造 recognizer 缺 `orchestration_prompt` 参数（破损点）
- **`_build_request_components()` L403-425**：`workspace-load` span 包裹 get/create；`create_workspace` 仅在 `get_workspace` 返回 None（首次）时调用，内部首次分支才调 `ws.initialize()`
- **`run()` ④ L714-747**：单个 span `intent-recognition` + 单次 `recognizer.recognize()` + 单个 `intents_recognized` 事件
- **`run()` ⑤ L749-758**：选编排器 + 加载状态（序号需顺延为 ⑥）
- 现有 SSE 事件：`query_rewritten` / `intents_recognized`（含 intents+relation）

### 已就绪基础设施（无需改动）
- `langfuse_service.start_span()`：context manager，借助 OTel context 自动嵌套到当前活跃 span
- `self._span(langfuse_service, name, input_dict)`：统一 span 创建（启用→真 span / 未启用→`_noop_ctx`）
- `_safe_update_span(span, output_dict)`：安全更新 span output
- chat_service.py 根 span `chat-response` 包裹 `orchestrator_service.run()`，子 span 自动嵌套

## 具体改动（Task 4 — 唯一待改文件：orchestrator_service.py）

### 改动 1：注入 orchestration_prompt（修复破损）

位置：`_build_request_components()` L458-464

```python
recognizer = IntentRecognizer(
    client=self._intent_client,
    model_config=self._intent_model_cfg,
    recognition_prompt=self._prompts.get("intent_recognition", ""),
    orchestration_prompt=self._prompts.get("intent_orchestration", ""),  # 新增
    intent_configs=intent_configs,
    default_orchestration=default_orchestration,
)
```

### 改动 2：workspace-initialize 子 span（首次创建时记录 initialize 耗时）

位置：`_build_request_components()` L411-425，将 `create_workspace` 调用包裹在嵌套 span 内

```python
with ws_ctx as ws_span:
    workspace = await self._workspace_manager.get_workspace(user_id_safe, session_id_safe)
    if workspace is None:
        # 首次创建：单独记录 initialize 耗时（create_workspace 内部仅首次分支调 ws.initialize()）
        with self._span(
            langfuse_service, "workspace-initialize",
            {"user_id": user_id_safe, "session_id": session_id_safe},
        ) as init_span:
            workspace = await self._workspace_manager.create_workspace(
                user_id=user_id_safe,
                session_id=session_id_safe,
                skill_dirs=all_skill_dirs,
            )
            _safe_update_span(init_span, {
                "workspace_id": getattr(workspace, "workspace_id", None),
            })
    if ws_span:
        try:
            ws_span.update(output={
                "workspace_id": getattr(workspace, "workspace_id", None),
            })
        except Exception:
            pass
```

trace 结构：首次 `chat-response → workspace-load → workspace-initialize`；复用仅 `chat-response → workspace-load`。

### 改动 3：run() 拆 ④ 识别 + ⑤ 编排（独立 span + 进度事件）

位置：`run()` L714-747，替换原单 span 单事件块为两步

```python
# ④ 意图识别（第一次 LLM，失败降级为 general_chat）
yield self._event({
    "type": "intent_step", "phase": "recognition", "status": "started",
    "message": "正在识别意图...",
})
with self._span(
    langfuse_service, "intent-recognition",
    {"query": rewritten, "history_len": len(history)},
) as rec_span:
    try:
        intents = await recognizer.recognize_intents(rewritten, history)
    except Exception:
        logger.exception("[OrchestratorService] 意图识别失败，降级为 general_chat")
        intents = [Intent(id="general_chat", query=rewritten, agent="general_agent")]
    _safe_update_span(rec_span, {
        "intents": [{"id": i.id, "agent": i.agent} for i in intents],
    })
yield self._event({
    "type": "intent_step", "phase": "recognition", "status": "done",
    "intents": [{"id": i.id, "query": i.query, "agent": i.agent} for i in intents],
})

# ⑤ 意图编排（第二次 LLM，失败降级为 independent）
yield self._event({
    "type": "intent_step", "phase": "orchestration", "status": "started",
    "message": "正在决策编排策略...",
})
with self._span(
    langfuse_service, "intent-orchestration",
    {"intents_count": len(intents)},
) as orch_span:
    try:
        relation, execution_order = await recognizer.plan_orchestration(rewritten, intents)
    except Exception:
        logger.exception("[OrchestratorService] 意图编排失败，降级为 independent")
        relation, execution_order = "independent", []
    _safe_update_span(orch_span, {
        "relation": relation, "execution_order": execution_order,
    })
# 按 execution_order 重排 intents（校验长度一致才应用，否则按原顺序）
if execution_order and len(execution_order) == len(intents):
    intents = [intents[i] for i in execution_order]
intent_result = IntentResult(
    rewritten_query=rewritten, intents=intents,
    relation=relation, execution_order=execution_order,
)
yield self._event({
    "type": "intents_recognized",
    "intents": [{"id": i.id, "agent": i.agent, "query": i.query} for i in intent_result.intents],
    "relation": intent_result.relation,
})

# ⑥ 选择编排器并加载各 agent 状态（原 ⑤，序号顺延）
mode = recognizer.get_orchestration_mode(intent_result)
...  # 后续不变
```

**事件时序变化**：
- 原：`intents_recognized`（一次，含 intents+relation）
- 新：`intent_step`(recognition started) → `intent_step`(recognition done, 含 intents) → `intent_step`(orchestration started) → `intents_recognized`(含重排后 intents + relation)

## 不改动的部分

- `recognizer.py`（Task 3 已完成，`recognize_intents`/`plan_orchestration`/`recognize`/`_parse_orchestration` 已就绪）
- `models.py` / `model_config.yml`（Task 1-2 已完成）
- `llm_client.py`（两次调用复用 `chat_complete`，进度事件由调用方 yield）
- `get_orchestration_mode()` 查表逻辑不变
- 三个编排器（pipeline/parallel/react）不变
- chat_service.py 不变（进度事件由 orchestrator_service 透传，chat_service 照常转发）
- 单 agent 路径不变（不走意图识别/编排）

## 假设与决策

1. **两次 LLM 都用非流式 chat_complete**。进度事件是环节级（started/done），非 token 级流式（用户已确认）。
2. **execution_order 用 0-based 索引排列**。长度需与 intents 一致才生效，否则忽略按原顺序。recognizer 内部已校验，run() 侧二次校验保险。
3. **workspace-initialize span 嵌套在 workspace-load 内**。仅首次创建出现，符合"单独记录首次初始化耗时"需求。
4. **intents_recognized 事件保留**：在两次 LLM 完成后统一发出（含重排后 intents + relation），保持与原前端契约兼容；新增 intent_step 为补充进度事件。

## 验证步骤（Task 5）

1. **AST 语法校验**：`python -c "import ast; ast.parse(open('app/services/orchestrator_service.py').read())"` + yaml 解析 model_config.yml
2. **导入校验**：确认 `IntentRecognizer` 构造参数匹配（orchestration_prompt 已注入）
3. **进度事件时序**：前端收到 `intent_step`(recognition started) → `intent_step`(recognition done) → `intent_step`(orchestration started) → `intents_recognized`
4. **span 嵌套**：Langfuse trace 显示 `intent-recognition` + `intent-orchestration` 两个独立子 span；首次请求显示 `workspace-load → workspace-initialize`
5. **降级路径**：第一次 LLM 失败 → intents 降级 general_chat；第二次失败 → relation 降级 independent
6. **git commit & push** 到 `origin/trae/agent-5CYjia`（token URL 方式）
