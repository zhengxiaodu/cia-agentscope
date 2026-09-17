# Checklist

- [ ] `BaseOrchestrator._run_single_agent` 签名改为 `async def -> AsyncGenerator[Union[str, TaskResult], None]`
- [ ] `_run_single_agent` 在 `async for event in agent.reply_stream` 循环中实时 `yield f"data: {event.model_dump_json()}\n\n"`
- [ ] `_run_single_agent` 循环结束后提取 output / final_state / success，最后 `yield result`（events 留空）
- [ ] `_run_single_agent` 异常分支捕获后 `yield result`（success=False）
- [ ] `TaskResult.events` 字段保留（向后兼容，默认空列表）
- [ ] PipelineOrchestrator 用 `asyncio.timeout` 包裹 `async for` 透传，超时构造失败 result
- [ ] PipelineOrchestrator 的 task_start / task_end / pipeline_intercept / summary 事件结构不变
- [ ] ParallelOrchestrator 用 `asyncio.Queue` 汇聚多 agent 事件，主循环交错 yield
- [ ] ParallelOrchestrator 单 agent 超时不影响其他 agent
- [ ] ParallelOrchestrator 的 task_start / task_end / summary 事件结构不变
- [ ] ReActOrchestrator 的 `_execute_action` 改为 async generator，实时 yield 事件 + 最后 yield observation
- [ ] ReActOrchestrator 的 react_step / react_act / react_observe / react_final / summary 事件结构不变
- [ ] `orchestrator_service.py` 无需改动（仅透传 orchestrator.run 的事件）
- [ ] `python -m py_compile` 4 个文件全部通过
- [ ] grep 核对：base.py 含 `yield f"data:`，三种编排器含 `async for item in self._run_single_agent`
- [ ] 改动已 commit 并 push 到 `origin/trae/agent-5CYjia`
