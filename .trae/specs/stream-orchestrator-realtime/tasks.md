# Tasks

- [ ] Task 1: 重构 `BaseOrchestrator._run_single_agent` 为 async generator
  - [ ] SubTask 1.1: 修改签名从 `async def -> TaskResult` 改为 `async def -> AsyncGenerator[Union[str, TaskResult], None]`
  - [ ] SubTask 1.2: 在 `async for event in agent.reply_stream` 循环中实时 `yield f"data: {event.model_dump_json()}\n\n"`
  - [ ] SubTask 1.3: 循环结束后提取 output / final_state / success，最后 `yield result`（events 留空）
  - [ ] SubTask 1.4: 异常分支捕获后设置 success=False 并 `yield result`
  - [ ] SubTask 1.5: 顶部导入 `Union`（typing）

- [ ] Task 2: Pipeline 编排器适配实时透传
  - [ ] SubTask 2.1: 把 `asyncio.wait_for(self._run_single_agent(...), timeout=...)` 改为 `async with asyncio.timeout(self._step_timeout):` 包裹 `async for` 循环
  - [ ] SubTask 2.2: `async for item in self._run_single_agent(...)`：若 isinstance(item, TaskResult) 则赋值给 result，否则 yield 透传事件
  - [ ] SubTask 2.3: 超时分支（asyncio.TimeoutError）构造 _make_timeout_result，与原逻辑一致
  - [ ] SubTask 2.4: 保留 task_start / task_end / pipeline_intercept / summary 事件结构不变

- [ ] Task 3: Parallel 编排器适配实时透传（Queue 模式）
  - [ ] SubTask 3.1: 引入 `asyncio.Queue`，定义内部 runner 协程：`async for item in self._run_single_agent(...)` 把非 TaskResult 项 await queue.put，最后 put TaskResult
  - [ ] SubTask 3.2: 每个 runner 用 `asyncio.wait_for(runner(), timeout=self._timeout)` 包装，异常时 put 一个 success=False 的 TaskResult
  - [ ] SubTask 3.3: 主循环：`asyncio.gather` 启动所有 runner task，同时主循环从 queue 取 item：TaskResult 收集到 self._last_results + 发 task_end，事件 yield 透传
  - [ ] SubTask 3.4: 所有 runner 完成后 await gather（return_exceptions=True），发送 summary
  - [ ] SubTask 3.5: 保留 task_start / task_end / summary 事件结构不变

- [ ] Task 4: ReAct 编排器适配实时透传
  - [ ] SubTask 4.1: `_execute_action` 改为 `async def -> AsyncGenerator[Union[str, str], None]`：实时 yield SSE 事件，最后 yield observation 字符串
  - [ ] SubTask 4.2: `call_agent` 分支：`async for item in self._run_single_agent(...)`，若 TaskResult 则提取 output + 收集到 self._last_results，否则 yield 透传
  - [ ] SubTask 4.3: `final` 分支与未知动作分支：直接 yield observation 字符串（无事件）
  - [ ] SubTask 4.4: `run()` 中 `observation = await self._execute_action(...)` 改为 `async for item in self._execute_action(...)`：若 str 且非事件则赋值给 observation，否则 yield 透传
  - [ ] SubTask 4.5: 保留 react_step / react_act / react_observe / react_final / summary 事件结构不变

- [ ] Task 5: 静态校验与验证
  - [ ] SubTask 5.1: `python -m py_compile` 对 4 个文件（base.py / pipeline.py / parallel.py / react.py）
  - [ ] 5.2: grep 核对 `yield f"data:` 在 base.py 的 `_run_single_agent` 中出现；三种编排器中 `async for item in self._run_single_agent` 出现
  - [ ] SubTask 5.3: 确认 `orchestrator_service.py` 无需改动（仅 `async for event in orchestrator.run(...)` 透传）
  - [ ] SubTask 5.4: git commit + push 到 `origin/trae/agent-5CYjia`

# Task Dependencies
- Task 2 / 3 / 4 均依赖 Task 1（_run_single_agent 改造完成）
- Task 2 / 3 / 4 之间无依赖，可并行实现
- Task 5 依赖 Task 1-4 全部完成
