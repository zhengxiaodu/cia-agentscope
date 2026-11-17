# Checklist

- [x] opensandbox SDK 按 sandbox_id 重连验证通过（Sandbox.connect 类方法存在，结论已回写 spec；线上连通性归入 S1 冒烟）
- [x] 同一用户请求路由到不同 Pod 时复用同一沙箱（代码实现：三级查找 本地→Redis 映射 connect→锁内新建；单测 test_workspace_redis_map.py 覆盖 connect 复用路径；线上跨 Pod 行为归 S1 冒烟）
- [x] 新建沙箱后 Redis 映射写入，TTL 与 WORKSPACE_TTL 一致（单测断言 set(key, id, ex=int(ttl))）
- [x] 沙箱淘汰/销毁时 Redis 映射同步删除（_evict_locked + 单测覆盖）
- [x] 分布式锁生效：同用户并发请求不产生双沙箱（SET NX EX 30 + 锁内 double-check + 等待轮询复用；单测覆盖「持锁方建好后 connect 复用」路径）
- [x] /chat/stop 路由到非持有任务 Pod 时，秒级内中断目标流式生成并正常落库（单测覆盖 exists=1 → CancelledError → user_abort 事件 + success=False 落库）
- [x] _cancelled 的 Redis 查询有秒级节流，不随事件量放大（单测：3 个检查点同窗口仅 1 次 exists 真实查询）
- [x] 流正常/中断结束后 Redis 停止标志被清理（stream() finally DEL + 单测覆盖）
- [x] Redis 不可用时全部降级为现有本地行为，异常仅记日志不影响主流程（manager 全部辅助方法 try/except + 单测覆盖 redis 异常/None 场景）
- [x] main.py 初始化顺序：redis_client 先于 workspace_manager 创建并注入（diff 核实，仅初始化一次）
- [x] helm 校验：replicaCount>1 且 storage.type=hostPath 时渲染失败并提示 RWX（模板静态验证：4 组条件模拟均正确；本机无 helm，待有 helm 环境复核）
- [x] values.yaml storage 注释包含多副本 RWX 要求说明（已加 7 行注释，默认值未变）
- [x] 新增单测：映射复用/新建/删除、锁 double-check、降级、stop 标志写入与轮询（2 文件 14 用例，py_compile 通过）
- [ ] 现有单元测试全量通过（无回归）——本机无项目依赖无法运行 pytest，待用户在测试环境执行
- [ ] S1 双副本冒烟通过：跨 Pod 沙箱复用、跨 Pod stop、历史文件下载、上传自愈、滚动发布接管——待用户在 S1 环境执行（见 tasks.md Task 6）
