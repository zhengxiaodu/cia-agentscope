# cia-agentscope Kubernetes Helm 部署设计

日期：2026-10-13

## 1. 背景与目标

为 `cia-agentscope` 准备一个可离线交付、可在现有 Kubernetes 集群中安装和升级的 Helm Chart。应用镜像由现有 Dockerfile 构建并推送至内网镜像仓库 `registry.ccdc.cn:5000`，Helm Chart 只负责应用工作负载，不负责镜像构建。

本次目标：

- 部署 `cia-agentscope` 应用服务；
- 使用现有外部 MySQL 与 Redis；
- 通过 `ClusterIP` 在集群内暴露服务；
- 将所有节点均已挂载的 `/app/dify/sfs` 作为共享持久化目录；
- 支持内网镜像仓库、已有 Secret、资源限制、健康检查、升级与回滚；
- 生成可带入内网的 Helm Chart 压缩包与部署说明。

## 2. 本次范围

### 2.1 包含

- 新增 `charts/cia-agentscope`；
- Deployment、Service、ConfigMap、可选 Secret、ServiceAccount 等模板；
- `hostPath` 共享存储挂载；
- 生产环境示例 values；
- Chart 校验、渲染和打包；
- 安装、升级、回滚与排查文档。

### 2.2 不包含

- OpenSandbox Server、Controller、CRD 及沙箱运行时的部署；
- MySQL、Redis 的部署；
- Ingress、域名、TLS 证书与外部负载均衡；
- 将应用当前的进程内状态改造成多副本共享状态；
- Kubernetes 集群和 `/app/dify/sfs` 底层共享文件系统的搭建。

OpenSandbox 后续独立部署。本次 Chart 只保留其连接参数，使应用可以在 OpenSandbox 就绪后通过修改 values 完成对接。

## 3. 总体架构

```text
集群内调用方
    |
    v
ClusterIP Service :7010
    |
    v
cia-agentscope Deployment（默认 1 个 Pod）
    |-- /health 探针
    |-- ConfigMap：非敏感环境变量
    |-- Existing Secret：密码、密钥
    |-- hostPath /app/dify/sfs
    |     `-- 容器内 /data/session-files
    |-- 外部 MySQL
    |-- 外部 Redis
    `-- 后续部署的 OpenSandbox
```

Chart 不创建 MySQL、Redis 和 OpenSandbox 资源，避免把不同生命周期的基础服务与应用发布绑定在一起。

## 4. Kubernetes 资源设计

### 4.1 Deployment

- 默认 `replicaCount: 1`；
- 容器端口为 `7010`；
- 镜像默认仓库为 `registry.ccdc.cn:5000/cia-agentscope`，tag 必须由发布方指定或使用 Chart 的 `appVersion`；
- 默认 `imagePullPolicy: IfNotPresent`；
- 支持 `imagePullSecrets`；
- 支持 requests/limits、nodeSelector、affinity、tolerations、podAnnotations 和 podLabels；
- 使用 RollingUpdate，并允许配置 `maxSurge`、`maxUnavailable`；
- 设置 `terminationGracePeriodSeconds`，让应用 lifespan 有时间关闭连接池和后台任务；
- 默认禁用权限提升并移除 Linux capabilities；pod/container securityContext 全部可配置。
- 当前 Dockerfile 未声明非 root 用户，为保证用户先构建的现有镜像能够启动，本次 Chart 不默认启用 `runAsNonRoot`。后续镜像增加固定 UID/GID 后，再在生产 values 中启用非 root 约束。

虽然 Chart 暴露 `replicaCount`，但当前应用包含 `app.state.chat_tasks`、反馈 `_pending`、后台 asyncio 任务和部分服务实例级状态。未完成状态外置、并发隔离和滚动升级验证前，生产默认保持单副本。

### 4.2 Service

- 类型固定默认为 `ClusterIP`；
- Service 端口与 targetPort 均默认为 `7010`；
- 不在本次 Chart 中创建 Ingress；
- 可通过 values 覆盖 Service annotations，便于后续接入集群内网关或服务治理组件。

### 4.3 健康检查

- startupProbe：`GET /health`，给模型配置加载和数据库初始化保留启动时间；
- readinessProbe：`GET /health`，未就绪时不接收流量；
- livenessProbe：`GET /health`，检测进程是否存活；
- 所有阈值、周期和超时时间均可在 values 中调整。

应用启动阶段会创建 MySQL 连接池并初始化表，因此 MySQL 不可用时 Pod 不会成功启动。部署说明必须把外部 MySQL 连通性和账号 DDL 权限列为前置检查项。

## 5. 配置与 Secret

### 5.1 非敏感配置

通过 ConfigMap 注入，主要包括：

- `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、`MYSQL_DATABASE`；
- `REDIS_URL`、`REDIS_SESSION_TTL`；
- `SESSION_FILES_PERSIST_DIR=/data/session-files`；
- `OPENSANDBOX_DOMAIN`、`OPENSANDBOX_PROTOCOL`、资源和池参数；
- 其余应用运行参数通过结构化 values 或 `extraEnv` 扩展。

不直接把整个本地 `.env` 复制进 Chart，避免把开发环境地址和凭证带入集群。

### 5.2 敏感配置

生产环境默认使用 `existingSecret`，至少提供：

- `MYSQL_PASSWORD`；
- `JWT_SECRET`；
- `CONFIG_ENCRYPTION_KEY`（使用加密配置时）；
- `OPENSANDBOX_API_KEY`（后续部署 OpenSandbox 后启用）；
- 其他模型、Langfuse 或外部系统凭证。

Chart 可提供 `secret.create` 作为开发或临时环境选项，但示例生产 values 不写明文密码。Secret key 名称应可配置，以便复用集群中已有 Secret。

## 6. SFS hostPath 存储设计

用户已确认每个可能调度应用 Pod 的节点都将同一个共享文件系统挂载在：

```text
/app/dify/sfs
```

Chart 使用：

```yaml
volumes:
  - name: shared-data
    hostPath:
      path: /app/dify/sfs
      type: Directory
```

容器内挂载到 `/data/session-files`，并设置：

```text
SESSION_FILES_PERSIST_DIR=/data/session-files
```

设计约束：

- `type: Directory`，目录缺失时让 Pod 启动失败，不由 Kubernetes 自动创建本地空目录；
- 上线前在所有可调度节点验证路径存在、确实指向同一个 SFS、读写权限一致；
- 支持 `storage.hostPath` 和 `storage.mountPath` 参数化，但生产示例固定使用上述路径；
- 支持通过 nodeSelector/affinity 将 Pod 限制到已完成 SFS 挂载验证的节点；
- hostPath 不提供容量申请、挂载编排或存储健康检查，底层 SFS 的高可用和备份由集群运维负责；
- Chart 保留未来切换 `existingClaim` 的结构，但本次验收以 hostPath 模式为准。

## 7. Values 接口

主要 values 结构：

```yaml
replicaCount: 1

image:
  repository: registry.ccdc.cn:5000/cia-agentscope
  tag: ""
  pullPolicy: IfNotPresent

imagePullSecrets: []

service:
  type: ClusterIP
  port: 7010

storage:
  type: hostPath
  hostPath: /app/dify/sfs
  mountPath: /data/session-files

externalMysql:
  host: ""
  port: 3306
  user: ""
  database: agentscope

externalRedis:
  url: ""

existingSecret:
  name: ""
  keys:
    mysqlPassword: MYSQL_PASSWORD
    jwtSecret: JWT_SECRET

opensandbox:
  domain: ""
  protocol: http
  apiKeySecretKey: OPENSANDBOX_API_KEY

resources: {}
nodeSelector: {}
tolerations: []
affinity: {}
extraEnv: []
extraEnvFrom: []
```

模板使用 `required` 或 values schema 对 MySQL、Redis、镜像 tag、existingSecret 等关键生产参数做尽早校验，避免生成表面合法但无法启动的清单。

## 8. 交付结构

计划新增：

```text
charts/
  cia-agentscope/
    Chart.yaml
    values.yaml
    values.schema.json
    templates/
      _helpers.tpl
      configmap.yaml
      deployment.yaml
      service.yaml
      serviceaccount.yaml
      secret.yaml
      NOTES.txt
    examples/
      values-prod-hostpath.yaml
    README.md
dist/
  cia-agentscope-<chart-version>.tgz
```

若最终镜像需要为 Kubernetes 精简，将另行增加 `Dockerfile.k8s`；它不是 Chart 安装的硬依赖。当前 Dockerfile 构建出的镜像只要能从集群节点拉取并监听 `7010`，即可先用于部署验证。

## 9. 验证与验收

实施阶段至少执行：

1. 使用生产示例 values 执行 `helm lint`；
2. 使用生产示例 values 执行 `helm template`，并验证缺少关键参数时能够尽早失败；
3. 对渲染结果执行 Kubernetes schema 校验（工具可用时）；
4. `helm package` 并生成 SHA256；
5. 检查渲染结果中不存在明文生产密码；
6. 检查 Service 为 ClusterIP、端口为 7010；
7. 检查 hostPath 为 `/app/dify/sfs`、容器挂载点为 `/data/session-files`；
8. 检查所有镜像均指向内网仓库；
9. 在目标集群进行安装、Pod 就绪、`/health`、SFS 读写、MySQL/Redis 连接和 Helm 回滚验证。

本机无法代替目标 Kubernetes 集群完成第 9 项，因此交付时要明确区分“静态验证通过”和“目标集群实装验证通过”。

## 10. 部署顺序

1. 构建应用镜像并推送到 `registry.ccdc.cn:5000`；
2. 验证所有候选节点的 `/app/dify/sfs`；
3. 准备应用 Secret；
4. 填写生产 values 中的镜像 tag、MySQL、Redis 和节点约束；
5. 使用 Helm 安装或升级应用；
6. 验证 Pod、Service、`/health` 和 SFS 读写；
7. OpenSandbox 独立部署完成后，更新其连接配置并再次执行 Helm upgrade。

## 11. 风险与后续事项

- **多副本一致性**：当前先单副本运行，后续需完成进程内状态外置和并发安全整改后再扩容；
- **hostPath 调度风险**：节点路径缺失或挂载异常会导致 Pod 启动失败或数据不一致，必须配合节点标签和巡检；
- **OpenSandbox 未部署**：依赖沙箱的功能暂不可用，基础健康检查不代表这些业务功能可用；
- **外部数据库权限**：应用启动会尝试初始化表，数据库账号需具备相应权限；
- **滚动升级中的长连接**：SSE 请求可能在 Pod 终止时中断，需要合理的优雅终止时间，并由客户端支持重连；
- **镜像用户权限**：SFS 目录权限必须允许最终容器用户读写，部署前需用实际镜像 UID/GID 验证。
