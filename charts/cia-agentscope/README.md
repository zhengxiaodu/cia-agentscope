# oia-agentscope Helm Chart

AgentScope 智能体服务（OIA-AGENTSCOPE）的**上线部署 Chart**。

结构与 OIA-CONFIG 的 `oia-config-local` 完全一致，沿用同一套集群约定（便于两个系统并行运维），
环境变量则按应用 `.env` 逐项拆解：

- **api**：FastAPI / AgentScope 后端，容器端口 `7012`，健康检查 `/health`
- **proxy**：前端由 Nginx 托管，容器端口 `80`，`/api/` 反代到后端 Service
- **对外暴露**：NodePort（前端 30102 / 后端 30713）

> 实测兼容性：Kubernetes **1.23.14** / Helm **v3.10.1**（`helm lint` + 多组渲染场景全部通过）。

## 1. 目录结构

```
deploy/helm/oia-agentscope/
├── Chart.yaml                 # name: agentscope（release 名建议用 oia-agentscope）
├── values.yaml                # 默认配置（含集群约定 + 应用 .env 全量变量）
├── values-prod.yaml           # 生产环境示例
├── values.schema.json         # 顶层键名校验，防 --set 写错键
├── .helmignore
├── README.md
└── templates/
    ├── _helpers.tpl           # 命名 / 标签 / PVC 名
    ├── config.tpl             # nginx.conf + 后端环境变量（多个 define）
    ├── configmap.yaml         # 前端 nginx.conf（名为 <release>）
    ├── deployment.yaml        # 前端 Nginx Deployment（名为 <release>）
    ├── service.yaml           # 前端 Service（名为 <release>-svc）
    ├── api-config.yaml        # 后端 ConfigMap（名为 <release>-api）
    ├── api-deployment.yaml    # 后端 Deployment
    ├── api-svc.yaml           # 后端 Service
    ├── pvc.yaml               # Nginx 日志 / 后端日志 / 会话文件 三个 PVC（外部技能默认不走 PVC）
    └── NOTES.txt              # 安装后打印访问地址
```

## 2. 资源命名（release = `oia-agentscope`）

| 资源 | 名称 | 说明 |
| --- | --- | --- |
| Deployment / ConfigMap（前端） | `oia-agentscope` | Nginx |
| Service（前端） | `oia-agentscope-svc` | NodePort 30102 |
| Deployment / Service / ConfigMap（后端） | `oia-agentscope-api` | 后端 |
| PVC（Nginx 日志） | `oia-agentscope` | 5Gi，nfs-client RWX |
| PVC（后端日志） | `oia-agentscope-api` | 20Gi，nfs-client RWX |
| PVC（会话文件） | `oia-agentscope-api-session-files` | 20Gi，nfs-client RWX |
| 外部技能目录 | 宿主机 `/app/dify/sfs/agentscope-external-skills` | hostPath（默认），跨命名空间共享 |

## 3. 端口规划

| 服务 | 容器端口 | Service 端口 | NodePort | 用途 |
| --- | --- | --- | --- | --- |
| proxy | 80 | 80 | **30102** | 页面；Nginx 同时反代 `/api`、`/health` |
| api | 7012 | 7012 | **30713** | API 直连，供其他系统调用 |

> oia-admin 用 30100/30711、oia-config 用 30101/30712，这里顺延用 30102/30713；
> 如需修改，注意 **nodePort 集群内全局唯一**。

## 4. 环境变量（对应应用 .env）

`templates/config.tpl` 里的 `oia-agentscope.api.config` 把 values 渲染成 `api-config` ConfigMap，
键名与应用 `.env` 完全对齐（.env 里没出现的变量一律不输出）：

| .env 变量 | values 路径 |
| --- | --- |
| `CONFIG_DECRYPT_KEY` | `api.configDecryptKey` |
| `JWT_SECRET` / `JWT_EXPIRE_HOURS` | `api.jwt.secret` / `api.jwt.expireHours` |
| `AUTH_MOCK` / `AUTH_API_URL` | `api.auth.mock` / `api.auth.apiUrl` |
| `REDIS_URL` / `REDIS_SESSION_TTL` | `externalRedis.*`（拼装） / `api.session.redisSessionTtl` |
| `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` | `externalMysql.*` |
| `UPLOAD_MAX_SIZE_MB` / `SESSION_FILES_PERSIST_DIR` | `api.upload.*` |
| `LANGFUSE_SECRET_KEY/PUBLIC_KEY/BASE_URL` | `langfuse.*` |
| `LOG_LEVEL/DIR/MAX_BYTES/BACKUP_COUNT` | `api.log.*` |
| `MNG_AUTH_URL` / `MNG_INTENT_URL` | `api.mng.*` |
| `EXTERNAL_SKILLS_DIR` | `api.skills.externalDir` |
| `OPENSANDBOX_*` / `WORKSPACE_TTL` | `api.opensandbox.*` |
| `REGULATIONS_*` / `POLICY_QA_KB_MAP` | `api.regulations.*` |
| `MINERU_API_KEY` / `MINERU_BASE_URL` | `api.mineru.*` |
| `SENSITIVE_*` | `api.sensitive.*` |
| `BOCHA_API_BASE` / `BOCHA_API_KEY` | `api.bocha.*` |

两处需要按环境确认（默认值已按集群改写，与 .env 的本地值不同）：

- `externalMysql`：.env 是 `localhost:3307` / `root`，集群里改成 `25.51.200.24:15001`，
  库名沿用 `agentscope`（需提前建库）。若该实例不允许 root 远程连接，换账号密码。
- `externalRedis`：.env 是 `redis://localhost:6379/0`，集群里拼成
  `redis://:****@redis.redis.svc.cluster.local:6379/0`。
- `api.regulations.kb.baseUrl`：.env 是 `http://localhost:6183`，集群里需改成 policy-kb-api 的实际 Service 地址。

`ENC(...)` 形式的 `JWT_SECRET` / `MYSQL_PASSWORD` 由应用用 `CONFIG_DECRYPT_KEY` 运行时解密，
所以 values 里保持 .env 原值即可；换环境时三者要一起重新生成。

## 5. 部署

### 5.1 构建并推送镜像

```bash
docker build -t registry.ccdc.cn:5000/oia/oia-agentscope-backend:1.0.0 .
docker push registry.ccdc.cn:5000/oia/oia-agentscope-backend:1.0.0

docker build -t registry.ccdc.cn:5000/oia/oia-agentscope-frontend:1.0.0 .
docker push registry.ccdc.cn:5000/oia/oia-agentscope-frontend:1.0.0
```

> 若只部署后端（无前端镜像），加 `--set proxy.enabled=false`。

### 5.2 准备命名空间与拉取密钥

```bash
kubectl create namespace oia

# 集群约定的拉取密钥名称：docker-register-secret
kubectl -n oia create secret docker-registry docker-register-secret \
  --docker-server=registry.ccdc.cn:5000 \
  --docker-username=<user> --docker-password=<pwd>
```

### 5.3 安装 / 升级

```bash
# 默认（values.yaml 中已固化集群约定）
helm upgrade --install oia-agentscope ./deploy/helm/oia-agentscope -n oia

# 生产（敏感项走 Secret）
kubectl -n oia create secret generic oia-agentscope-secret \
  --from-literal=CONFIG_DECRYPT_KEY='<加密主密钥>' \
  --from-literal=JWT_SECRET='<该环境独立生成的签名密钥或密文>' \
  --from-literal=MYSQL_PASSWORD='ENC(...)' \
  --from-literal=REDIS_URL='redis://:password@host:6379/0' \
  --from-literal=LANGFUSE_PUBLIC_KEY='pk-lf-xxx' \
  --from-literal=LANGFUSE_SECRET_KEY='sk-lf-xxx' \
  --from-literal=OPENSANDBOX_API_KEY='<沙箱密钥>' \
  --from-literal=MINERU_API_KEY='<MinerU 密钥>' \
  --from-literal=BOCHA_API_KEY='<博查密钥>'

helm upgrade --install oia-agentscope ./deploy/helm/oia-agentscope \
  -n oia -f ./deploy/helm/oia-agentscope/values-prod.yaml
```

其他命令：

```bash
helm lint ./deploy/helm/oia-agentscope                  # 语法检查
helm template oia-agentscope ./deploy/helm/oia-agentscope   # 本地渲染预览
helm -n oia uninstall oia-agentscope
```

## 6. 关键设计

### 6.1 nginx.conf 整体挂载

Chart 生成的**完整 nginx.conf** 挂载到 `/etc/nginx/nginx.conf`（`subPath: nginx.conf`），
而不是 conf.d 片段 —— 因为配置含 `worker_processes` / `events`，这两个指令只在 main 上下文合法。

生成的主配置**不包含** `include /etc/nginx/conf.d/*.conf;`，因此镜像内置的 `default.conf`
不会被加载，不会出现端口冲突；`/api/` 的上游由模板动态拼成 `<release>-api:7012`。

### 6.2 敏感项注入

设置 `api.existingSecret` 后，`api.existingSecretKeys` 里**非空**的键对应的变量不再写入
ConfigMap，改为 `secretKeyRef` 注入：

`CONFIG_DECRYPT_KEY` / `JWT_SECRET` / `MYSQL_PASSWORD` / `REDIS_URL` /
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `OPENSANDBOX_API_KEY` /
`REGULATIONS_KB_INTERNAL_TOKEN` / `REGULATIONS_LANGFUSE_PUBLIC_KEY` /
`REGULATIONS_LANGFUSE_SECRET_KEY` / `MINERU_API_KEY` / `BOCHA_API_KEY`

对应值留空即不注入该项（Secret 里没这个键时很有用），此时变量仍按明文写进 ConfigMap。
判断逻辑集中在 `config.tpl` 的 `oia-agentscope.api.env` 与 `api-deployment.yaml` 的
`$secretMap`，两处共用同一套键名，改的时候要同步。

### 6.3 数据库与 Redis

- MySQL：由 `externalMysql.*` 渲染成 `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE`（本项目不用 `DATABASE_URL`）。
- Redis：由 `externalRedis.*` 拼装成 `REDIS_URL`，密码经 `urlquery` 转义
  （`Welcome!23$%` → `Welcome%2123%24%25`），不会被 `#`、`@`、`%` 截断；
  也可直接给 `externalRedis.url` 覆盖整串。

### 6.4 健康探针

| 组件 | 路径 | 说明 |
| --- | --- | --- |
| api | `/health` | 后端自带，无需鉴权 |
| proxy | `/nginx-healthz` | Nginx 自身返回 200，不依赖后端 |

后端默认开启 `startupProbe`（5s × 60 次容错），避免首次启动建表/拉镜像耗时被杀。

### 6.5 持久化

三个目录都需要多副本共享，默认 `nfs-client` + `ReadWriteMany`，并带
`helm.sh/resource-policy: keep`（卸载 release 时保留数据）：

| values 路径 | 挂载点 | 对应变量 |
| --- | --- | --- |
| `api.persistence.logs` | `/app/logs` | `LOG_DIR` |
| `api.persistence.sessionFiles` | `/app/data/session_files` | `SESSION_FILES_PERSIST_DIR` |
| `api.persistence.externalSkills` | `/app/external_skills` | `EXTERNAL_SKILLS_DIR` |

**外部技能目录默认不用 PVC**，而是直接挂载宿主机上的 SFS 目录：

```yaml
api:
  persistence:
    externalSkills:
      enabled: true
      type: hostPath          # hostPath（默认）| pvc
      mountPath: "/app/external_skills"
      hostPath:
        path: "/app/dify/sfs"
        type: DirectoryOrCreate   # 目录不存在会自动创建
        subPath: "agentscope-external-skills"
```

原因：**一个 PV 只能被一个 PVC 绑定**，`pvc` 方案无法让其它命名空间的服务读写同一份技能文件；
hostPath 则由各命名空间的 Pod 各自挂载同一个宿主机目录，天然共享。

> 前置条件：Pod 可调度到的节点上都已挂载该 SFS（当前 `nodeSelector group: dify` 的节点组）。
> 挂载点缺失时 Pod 会一直 `ContainerCreating`，`kubectl describe` 里可见
> `hostPath type check failed` 或 `no such file or directory`。

## 7. 与 `oia-config-local` 的差异

| 维度 | oia-config-local | oia-agentscope（本 Chart） |
| --- | --- | --- |
| 应用变量 | APP_NAME / SECRET_KEY / DATABASE_URL / SKILLS_DIR 等 | 按应用 .env：CONFIG_DECRYPT_KEY / JWT_* / MYSQL_* / OPENSANDBOX_* / REGULATIONS_* 等 |
| 数据库注入 | `DATABASE_URL` 单串 | `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` 分段 |
| Redis 注入 | `REDIS_HOST/PORT/DB/PASSWORD` | `REDIS_URL` 单串 |
| 持久化 | 日志 PVC + 技能目录 | 日志 / 会话文件 两个 PVC + 外部技能目录 |
| NodePort | 前端 30101 / 后端 30712 | 前端 30102 / 后端 30713 |

其余（镜像仓库、拉取密钥、nodeSelector、存储类、nginx 反代、探针、Secret 注入方式）完全一致。

## 8. 故障排查

```bash
kubectl -n oia get pods,svc,pvc -l app.kubernetes.io/instance=oia-agentscope
kubectl -n oia logs -l app.kubernetes.io/instance=oia-agentscope,component=api --tail=200
kubectl -n oia get cm oia-agentscope-api -o yaml | head -60

NODE_IP=<节点IP>
curl http://$NODE_IP:30102            # 页面
curl http://$NODE_IP:30713/health     # 后端健康
```

常见问题：

- `ImagePullBackOff` → `docker-register-secret` 未创建或镜像 tag 不存在。
- `provided port is already allocated` → nodePort 与集群内其他 Service 冲突，换端口或置空自动分配。
- Nginx `host not found in upstream` → 后端 Service 未创建（检查 `api.enabled`）。
- 页面可开但接口 502 → 后端 Pod 未 Ready，检查 MySQL / Redis 连通性。
- 后端起不来并报解密失败 → `CONFIG_DECRYPT_KEY` 与 `ENC(...)` 的 `JWT_SECRET` / `MYSQL_PASSWORD` 不是同一套。
- 会话文件重启后丢失 → 确认 `api.persistence.sessionFiles.enabled=true`，且
  `SESSION_FILES_PERSIST_DIR` 与挂载点一致。
- Pod 卡在 `ContainerCreating` → `kubectl describe pod` 看 Events，多为宿主机 SFS 目录不存在
  或未挂载（对照 6.5 的前置条件）。
