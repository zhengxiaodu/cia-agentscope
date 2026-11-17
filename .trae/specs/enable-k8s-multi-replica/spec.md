# K8s 多后端副本部署改造 Spec

## Why

后端当前以 K8s 单副本运行，需扩容为多副本负载均衡。代码中存在三处进程内状态，多副本下会导致功能异常：沙箱映射（同用户跨 Pod 双沙箱、agent 产物丢失）、/chat/stop 取消标志（跨 Pod 无法中断）、session_files 持久化目录（hostPath 节点本地、跨节点读写分离 404）。

## 排查结论（现状盘点）

**阻断点（必须修复）：**
1. `opensandbox_workspace_manager.py` `self._cache`（user_id → Sandbox 进程内存映射）：同一用户请求被路由到不同 Pod 时各自创建沙箱 → agent 生成的产物文件（图表/文档）跨 Pod 丢失、沙箱数量翻倍
2. `main.py:164` `app.state.chat_tasks`（/chat/stop 取消标志）：停止请求落到非持有任务的 Pod 时无法中断流式回复
3. `values.yaml` storage.type 默认 hostPath（节点本地目录）：多副本调度到不同节点时，session_files 写读分离 → `/files/persisted` 下载 404

**已确认无问题（共享存储支撑多副本）：**
- AgentState：MySQL `agent_states` 表（每轮加载/保存）
- 会话/消息/上传元数据与解析内容：MySQL
- 用户融合配置：Redis `user_config:{user_id}`
- 鉴权：JWT 无状态 + Redis 共享；Langfuse 各 Pod shutdown 自行 flush；无 WebSocket；RoundState 已按请求隔离

**风险点（缓解/记录，不单独开发）：**
- 同用户并发请求跨 Pod 同时建沙箱 → 由分布式锁一并解决
- 预热池 per-Pod（总沙箱数 = pool_size × 副本数）→ 部署侧调小 OPENSANDBOX_POOL_SIZE
- MinerU 后台解析为接收上传 Pod 的进程内 asyncio 任务，Pod 重启任务丢失（单副本已存在，非多副本引入，已有超时文案兜底）

## What Changes

1. **沙箱映射外置 Redis**：`OpenSandboxWorkspaceManager` 新增可选 `redis_client` 依赖；`create_workspace`/`get_workspace` 在本地 cache 未命中时查 Redis `sandbox_map:{user_id}`（存 sandbox_id，TTL=WORKSPACE_TTL，命中且 connect 成功则复用并刷新 TTL）；新建/销毁沙箱时同步写/删 Redis 键；本地 `_cache` 保留为进程内加速层
2. **分布式锁**：新建沙箱临界区用 Redis `SET sandbox_lock:{user_id} NX EX 30` 包裹，防止同用户并发请求跨 Pod 双建
3. **跨 Pod 停止**：`/chat/stop` 除 set 本地 event 外，写 Redis `chat_stop:{session_id}`（EX 300）；`generate_response` 的 `_cancelled()` 扩展为「本地 event 已 set 或 Redis 标志存在」，Redis 查询按秒级节流避免每事件查询
4. **main.py 初始化顺序**：Redis client 初始化提前到 workspace_manager 之前并注入
5. **Helm 防护**：deployment.yaml 增加校验——`replicaCount > 1` 且 `storage.type == hostPath` 时模板 fail 并提示需 ReadWriteMany 存储；values.yaml 相关注释补充多副本要求
6. **降级语义**：Redis 不可用时全部回退现有本地行为（本地 cache / 本地 stop / 原部署方式），不劣于现状

**BREAKING**：无（Redis 不可用时行为与现状一致；多副本部署必须将 storage 切换为 RWX PVC，由 helm 校验强制提示）

## Impact

- Affected code：`app/services/opensandbox_workspace_manager.py`、`app/routes/chat.py`、`app/services/chat_service.py`、`app/main.py`、`charts/cia-agentscope/templates/deployment.yaml`、`charts/cia-agentscope/values.yaml`
- 依赖前置：✅ 已验证 opensandbox SDK 支持 `Sandbox.connect(sandbox_id, connection_config=...)` 类方法（SDK 源码 sandbox.py L864，签名含 health_check/connect_timeout/skip_health_check），按 ID 重连可行
- 测试：workspace manager 新增单测（映射读写/锁/降级），chat stop 跨实例单测

## ADDED Requirements

### Requirement: 跨 Pod 沙箱复用
系统 SHALL 在多副本部署下，使同一用户的请求无论路由到哪个 Pod，均复用同一个沙箱工作区（通过 Redis 共享 user_id → sandbox_id 映射 + SDK 按 id 重连）。

#### Scenario: 请求路由切换 Pod 后复用沙箱
- **WHEN** 用户 A 的对话请求依次落在 Pod1、Pod2
- **THEN** Pod2 通过 Redis 映射拿到 Pod1 创建的 sandbox_id 并成功重连，两轮对话工作目录内容（含 agent 产物）连续

#### Scenario: 沙箱淘汰后映射同步清理
- **WHEN** 任一 Pod 淘汰/销毁某用户沙箱
- **THEN** Redis 中对应 `sandbox_map` 键同步删除，下次请求创建新沙箱并由自愈机制补写上传文件

#### Scenario: 并发请求不双建
- **WHEN** 同一用户的两个请求同时落在不同 Pod 且均未命中映射
- **THEN** 分布式锁保证仅创建一个沙箱，后获取锁的请求复用先创建的结果

### Requirement: 跨 Pod 停止生成
系统 SHALL 使 `/chat/stop` 在多副本下无论路由到哪个 Pod 均能中断目标会话的流式生成（通过 Redis 停止标志 + 生成侧轮询）。

#### Scenario: 停止请求落在非持有任务的 Pod
- **WHEN** 会话流式生成运行在 Pod1，`/chat/stop` 请求路由到 Pod2
- **THEN** Pod1 的生成器在秒级内检测到 Redis 停止标志，按现有用户中断路径落库（success=False）并结束流

### Requirement: 多副本部署配置防护
Chart SHALL 在 `replicaCount > 1` 且 `storage.type == hostPath` 时拒绝渲染并给出 ReadWriteMany 迁移提示。

#### Scenario: 多副本 + hostPath 配置
- **WHEN** helm 渲染 `replicaCount: 2` 且 `storage.type: hostPath`
- **THEN** 模板校验失败，错误信息指引切换 `storage.type: pvc`（ReadWriteMany）

## MODIFIED Requirements

### Requirement: Redis 故障降级
`OpenSandboxWorkspaceManager` 与停止机制 SHALL 在 Redis 不可用时降级为现有单进程行为：本地 cache 查找、本地取消标志生效、Redis 异常仅记日志不影响主流程。
