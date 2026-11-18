# Tasks

- [x] Task 1: 验证 opensandbox SDK 按 sandbox_id 重连能力
  - [x] SDK 源码验证：`Sandbox.connect(sandbox_id, connection_config=..., skip_health_check=...)` 类方法存在（本地安装最新 SDK 检查 sandbox.py L864），支持按 ID 重连；线上连通性验证合并到 Task 6 双副本冒烟
  - [x] 结论已回写 spec：支持 → 执行 Task 2-5
- [x] Task 2: 沙箱映射外置 Redis + 分布式锁（opensandbox_workspace_manager.py）
  - [x] 构造函数新增可选 `redis_client` 参数；Redis 键 `sandbox_map:{user_id}` 存 sandbox_id，TTL=WORKSPACE_TTL，复用时刷新
  - [x] `create_workspace`/`get_workspace`：本地 `_cache` 未命中 → 查 Redis → `Sandbox.connect` 重连并放入本地 cache；均未命中 → 在 Redis 锁 `sandbox_lock:{user_id}`（SET NX EX 30）内新建，锁内 double-check 映射防止双建；新建后写 Redis 映射
  - [x] `_evict_locked` 销毁沙箱时删除 Redis 映射键；所有 Redis 操作 try/except 降级（失败仅告警，走本地逻辑）
  - [x] `main.py`：Redis client 初始化提前至 workspace_manager 之前并注入构造函数
- [x] Task 3: 跨 Pod 停止生成（chat.py + chat_service.py）
  - [x] `/chat/stop`：本地 chat_tasks 命中 set event（现有逻辑）之外，无条件写 Redis `chat_stop:{session_id}`（EX 300），返回语义保持
  - [x] `generate_response` 的 `_cancelled()`：本地 event 已 set 或 Redis 停止标志存在；Redis 查询节流（同 1 秒内最多查一次，用闭包内 monotonic 时间戳实现）
  - [x] 流结束 finally 中删除 Redis 停止标志键（避免残留影响后续同 session 请求）
  - [x] redis_client 经 request.app.state 传入，异常降级仅记日志
- [x] Task 4: Helm 防护与 values 注释（charts/cia-agentscope）
  - [x] deployment.yaml：`replicaCount > 1` 且 `storage.type == hostPath` 时 `fail` 提示切换 ReadWriteMany
  - [x] values.yaml storage 段注释补充多副本要求（RWX / 建议 pvc+nfs-client，参考 examples/values-prod-pvc.yaml）
- [x] Task 5: 单元测试
  - [x] workspace manager：Redis 映射命中复用（mock connect）、未命中新建并写映射、锁内 double-check、销毁删映射、Redis 异常降级本地（tests/test_workspace_redis_map.py，7 用例，py_compile 通过；本机无项目依赖，pytest 待测试环境执行）
  - [x] chat stop：Redis 标志写入、_cancelled 节流轮询、流结束清理标志（tests/test_chat_stop_redis_flag.py，7 用例，py_compile 通过）
  - [ ] 回归：现有测试全量通过（本机无依赖无法运行，待用户在测试环境执行 pytest）
- [ ] Task 6: 双副本部署验证（S1 环境，用户执行）
  - [ ] values：replicaCount=2 + storage.type=pvc（RWX）+ OPENSANDBOX_POOL_SIZE 减半
  - [ ] 冒烟：同用户连续对话（观察跨 Pod 复用同一 sandbox_id）、跨 Pod stop 中断、历史产物文件下载、上传文件自愈
  - [ ] 滚动发布：销毁一个 Pod 验证另一 Pod 接管（AgentState 从 MySQL 恢复、上传文件自愈）

# Task Dependencies
- Task 2-5 依赖 Task 1 的「支持 connect」结论
- Task 5 依赖 Task 2、3（测试对象）；Task 4 独立可并行
- Task 6 依赖 Task 1-5 全部完成
