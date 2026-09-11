# Tasks

- [x] Task 1: Upload 路由自动获取 session_id
  - [x] `app/routes/upload.py` 函数签名增加 `request: Request` 参数
  - [x] 当 `session_id` 为空时，通过 `request.app.state.session_service.get_or_create_session(None, user_id)` 获取新 session_id
  - [x] 已传 session_id 时行为不变
  - [x] `py_compile` 通过

- [x] Task 2: OrchestratorService 内置工具替换
  - [x] `app/services/orchestrator_service.py` 顶部新增 `from agentscope.tool import Bash, Read, Write, Edit, Glob, Grep`
  - [x] 将 `all_tools = await workspace.list_tools()` 替换为 `all_tools = [Bash(), Read(), Write(), Edit(), Glob(), Grep()]`
  - [x] 删除原有的 `all_tools = await workspace.list_tools()` 行
  - [x] `py_compile` 通过

- [x] Task 3: 文件扫描排除 skills/ 和 .mcp
  - [x] `app/services/file_change_detector.py` 中 `_EXCLUDED_TOP_DIRS` 从 `{"data"}` 扩展为 `{"data", "skills"}`
  - [x] `snapshot` 函数中增加过滤：遍历 `files` 时跳过文件名为 `.mcp` 的文件
  - [x] `py_compile` 通过

- [x] Task 4: 新增清理配置项
  - [x] `app/config.py` 新增 `WORKSPACE_RETENTION_DAYS = int(os.getenv("WORKSPACE_RETENTION_DAYS", "7"))` 和 `WORKSPACE_CLEANUP_INTERVAL_HOURS = int(os.getenv("WORKSPACE_CLEANUP_INTERVAL_HOURS", "24"))`
  - [x] `.env` 和 `.env.example` 新增 `WORKSPACE_RETENTION_DAYS=7` 和 `WORKSPACE_CLEANUP_INTERVAL_HOURS=24`
  - [x] `py_compile` 通过

- [x] Task 5: 实现定时工作区清理服务
  - [x] 新增 `app/services/workspace_cleanup_service.py`
  - [x] 实现 `WorkspaceCleanupService` 类，构造参数：`basedir: str, retention_days: int, interval_hours: int`
  - [x] `cleanup()` 方法：扫描 basedir 下所有直接子目录，对每个子目录检查 `os.path.getmtime` 是否超过 retention_days 天，超过则 `shutil.rmtree` 删除；每个目录删除记录 logger.info，失败记 logger.warning 但不中断后续
  - [x] `start()` 方法：用 `apscheduler.schedulers.asyncio.AsyncIOScheduler` + `interval_trigger(hours=interval_hours)` 注册 cleanup 任务
  - [x] `stop()` 方法：`scheduler.shutdown(wait=False)`
  - [x] `py_compile` 通过

- [x] Task 6: main.py lifespan 接入清理服务
  - [x] 导入 `WorkspaceCleanupService` 和 `WORKSPACE_RETENTION_DAYS`、`WORKSPACE_CLEANUP_INTERVAL_HOURS`
  - [x] 启动阶段：构造 `WorkspaceCleanupService(basedir=WORKSPACE_BASEDIR, retention_days=WORKSPACE_RETENTION_DAYS, interval_hours=WORKSPACE_CLEANUP_INTERVAL_HOURS)`，调用 `start()`
  - [x] 关闭阶段：调用 `stop()`
  - [x] `py_compile` 通过

- [x] Task 7: 静态核对
  - [x] 全部改动文件 `py_compile` 通过
  - [x] grep 确认无残留引用
  - [x] 核对 checklist 全部条目

# Task Dependencies
- Task 1 独立
- Task 2 独立
- Task 3 独立
- Task 5 依赖 Task 4（需配置项）
- Task 6 依赖 Task 4 + Task 5（需配置项 + 服务类）
- Task 7 依赖 Task 1-6 全部完成
