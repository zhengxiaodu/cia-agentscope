# OpenSandbox 集群部署手册 v2.0

> 基于 v1.0 补充完整组件：CRD、Controller Manager、Ingress Gateway、BatchSandbox 模板、RBAC 修正、镜像同步完整清单、离线部署支持。

---

## 1. 架构说明

OpenSandbox 0.2.x 集群版至少包含以下组件：

```text
┌─────────────┐     ┌─────────────────────┐     ┌──────────────┐
│   Client    │────▶│  OpenSandbox Server │────▶│  BatchSandbox│
│  (SDK/curl) │     │   (API Gateway)     │     │     CRD      │
└─────────────┘     └─────────────────────┘     └──────┬───────┘
                                                       │
                              ┌───────────────────────┘
                              ▼
                    ┌─────────────────────┐
                    │ Controller Manager  │────▶ 创建/管理沙箱 Pod
                    └─────────────────────┘
```

| 组件 | 是否必须 | 说明 |
| --- | --- | --- |
| CRDs | 是 | `batchsandboxes`、`pools`、`sandboxsnapshots` |
| Server | 是 | API 网关，接收 SDK/客户端请求 |
| Controller Manager | 是 | 监听 CRD，创建实际沙箱 Pod |
| Ingress Gateway | 否 | 直连模式需要，Server Proxy 模式不需要 |
| BatchSandbox 模板 | 是 | Server 创建 CR 时使用 |

---

## 2. 前置条件

- Kubernetes >= 1.24
- kubectl 已配置集群访问
- 节点可用内存 >= 4Gi（每个沙箱 Pod 128Mi–512Mi）
- 内网可访问的镜像仓库（Docker Registry / Harbor / ACR 等）
- 镜像仓库认证信息（如仓库为私有）

---

## 3. 内网镜像同步

### 3.1 完整镜像清单

| 源镜像 | 用途 | 大小 | 备注 |
| --- | --- | --- | --- |
| `sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/server:v0.2.2` | Server API 网关 | ~200MB | 必须 |
| `sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/controller:v0.2.0` | Controller Manager | ~150MB | 必须 |
| `sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/ingress:v1.0.10` | Ingress Gateway | ~100MB | 可选 |
| `sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/execd:v1.0.21` | 沙箱内执行代理 | ~50MB | 必须 |
| `sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/egress:v1.1.5` | 出口网关 | ~100MB | 必须 |
| `sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/image-committer:v0.1.1` | 镜像提交 Job | ~100MB | 必须 |
| `sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.1.0` | Code Interpreter | ~7GB | 默认不同步；需 Code Interpreter 时加 `--with-code-interpreter` |
| `python:3.13-slim` | 沙箱基础镜像 | ~150MB | 客户内网已有可跳过 |

### 3.2 同步脚本

```bash
#!/bin/bash
# sync_opensandbox_images.sh - 将 OpenSandbox 镜像同步到内网镜像仓库
# 支持：Harbor / Docker Registry / 其他兼容 Docker Registry V2 的仓库
# 用法: ./sync_opensandbox_images.sh <registry-address> [--skip-python] [--with-code-interpreter]
# 示例:
#   ./sync_opensandbox_images.sh docker-registry.internal.example.com
#   ./sync_opensandbox_images.sh docker-registry.internal.example.com --skip-python
#   ./sync_opensandbox_images.sh docker-registry.internal.example.com --skip-python --with-code-interpreter

set -e

TARGET_REGISTRY="${1:?用法: $0 <registry-address> [--skip-python] [--with-code-interpreter]}"
SOURCE_REGISTRY="sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com"
PROJECT="opensandbox"

SKIP_PYTHON=false
WITH_CODE_INTERPRETER=false
for arg in "${@:2}"; do
    if [[ "$arg" == "--skip-python" ]]; then
        SKIP_PYTHON=true
    elif [[ "$arg" == "--with-code-interpreter" ]]; then
        WITH_CODE_INTERPRETER=true
    fi
done

IMAGES=(
    "server:v0.2.2"
    "controller:v0.2.0"
    "ingress:v1.0.10"
    "execd:v1.0.21"
    "egress:v1.1.5"
    "image-committer:v0.1.1"
)

echo "==> 源镜像仓库: ${SOURCE_REGISTRY}"
echo "==> 目标镜像仓库: ${TARGET_REGISTRY}"
echo "==> 推送路径示例: ${TARGET_REGISTRY}/${PROJECT}/server:v0.2.2"
if [[ "$SKIP_PYTHON" == "true" ]]; then
    echo "==> 跳过 python:3.13-slim（客户内网已有）"
fi
if [[ "$WITH_CODE_INTERPRETER" == "false" ]]; then
    echo "==> 默认跳过 code-interpreter:v1.1.0（镜像约 7GB，启用请添加 --with-code-interpreter）"
fi
echo ""

# 检查是否已登录目标仓库
if ! docker info >/dev/null 2>&1; then
    echo "错误：Docker 未运行或未配置"
    exit 1
fi

echo "==> 开始同步 OpenSandbox 组件镜像..."
for img in "${IMAGES[@]}"; do
    echo "==> 同步 ${img}"
    docker pull "${SOURCE_REGISTRY}/${PROJECT}/${img}"
    docker tag "${SOURCE_REGISTRY}/${PROJECT}/${img}" "${TARGET_REGISTRY}/${PROJECT}/${img}"
    docker push "${TARGET_REGISTRY}/${PROJECT}/${img}"
done

if [[ "$WITH_CODE_INTERPRETER" == "true" ]]; then
    echo "==> 同步 code-interpreter:v1.1.0"
    docker pull "${SOURCE_REGISTRY}/${PROJECT}/code-interpreter:v1.1.0"
    docker tag "${SOURCE_REGISTRY}/${PROJECT}/code-interpreter:v1.1.0" "${TARGET_REGISTRY}/${PROJECT}/code-interpreter:v1.1.0"
    docker push "${TARGET_REGISTRY}/${PROJECT}/code-interpreter:v1.1.0"
fi

if [[ "$SKIP_PYTHON" != "true" ]]; then
    echo "==> 同步 python:3.13-slim"
    docker pull python:3.13-slim
    # Docker Registry 路径：docker-registry.example.com/library/python:3.13-slim
    # Harbor 路径：harbor.example.com/library/python:3.13-slim
    docker tag python:3.13-slim "${TARGET_REGISTRY}/library/python:3.13-slim"
    docker push "${TARGET_REGISTRY}/library/python:3.13-slim"
fi

echo ""
echo "==> 全部同步完成"
echo ""
echo "部署时请替换以下文件中的镜像地址为内网仓库地址："
echo "  - server-config.yaml (runtime.execd_image, egress.image)"
echo "  - controller-manager-deployment.yaml (--image-committer-image, image)"
echo "  - ingress-gateway-deployment.yaml (image)"
echo "  - server-deployment.yaml (image)"
if [[ "$SKIP_PYTHON" == "true" ]]; then
    echo ""
    echo "注意：已跳过 python:3.13-slim，请确保客户内网仓库存在该镜像，"
    echo "      或在沙箱创建时指定其他可用的 python 镜像。"
fi
if [[ "$WITH_CODE_INTERPRETER" == "false" ]]; then
    echo ""
    echo "注意：已跳过 code-interpreter:v1.1.0。如需使用 Code Interpreter 功能，"
    echo "      请重新执行脚本并添加 --with-code-interpreter 参数。"
fi

```

### 3.3 Docker Registry 特别说明

如果客户使用 Docker Registry，同步前需先登录：

```bash
docker login docker-registry.internal.example.com
```

如果 Docker Registry 使用 HTTP（非 HTTPS），需在每个节点配置 `/etc/docker/daemon.json`：

```json
{"insecure-registries": ["docker-registry.internal.example.com"]}
```

然后重启 Docker：

```bash
sudo systemctl restart docker
```

同步后的镜像路径为：

```text
docker-registry.internal.example.com/opensandbox/server:v0.2.2
docker-registry.internal.example.com/library/python:3.13-slim
```

### 3.4 同步后镜像地址替换

修改 `server-config.yaml`：

```toml
[runtime]
execd_image = "docker-registry.internal.example.com/opensandbox/execd:v1.0.21"

[egress]
image = "docker-registry.internal.example.com/opensandbox/egress:v1.1.5"
```

修改 `controller-manager-deployment.yaml`：

```yaml
# Controller Manager 镜像
image: docker-registry.internal.example.com/opensandbox/controller:v0.2.0
# image-committer 镜像参数
- --image-committer-image=docker-registry.internal.example.com/opensandbox/image-committer:v0.1.1
```

修改 `server-deployment.yaml`：

```yaml
image: docker-registry.internal.example.com/opensandbox/server:v0.2.2
```

修改 `ingress-gateway-deployment.yaml`（如使用）：

```yaml
image: docker-registry.internal.example.com/opensandbox/ingress:v1.0.10
```

如果跳过了 `python:3.13-slim`，还需在应用和测试环境设置：

```bash
export OPENSANDBOX_IMAGE="docker-registry.internal.example.com/library/python:3.13-slim"
export WORKSPACE_BASE_IMAGE="docker-registry.internal.example.com/library/python:3.13-slim"
```

---

## 4. K8s 部署

### 4.1 创建命名空间

```yaml
# namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: opensandbox-system
  labels:
    app.kubernetes.io/part-of: opensandbox
---
apiVersion: v1
kind: Namespace
metadata:
  name: opensandbox
  labels:
    app.kubernetes.io/part-of: opensandbox
```

```bash
kubectl apply -f namespace.yaml
```

### 4.2 安装 CRDs

```bash
kubectl apply -f crds.yaml
```

验证：

```bash
kubectl get crd | grep sandbox.opensandbox.io
```

预期输出包含：

```text
batchsandboxes.sandbox.opensandbox.io
pools.sandbox.opensandbox.io
sandboxsnapshots.sandbox.opensandbox.io
```

### 4.3 配置 API Key

```yaml
# api-key-secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: opensandbox-api-key
  namespace: opensandbox-system
type: Opaque
stringData:
  # 生产环境请使用随机生成的 64 位 hex 字符串
  api-key: "YOUR_64_CHAR_HEX_API_KEY_HERE"
```

生成并应用：

```bash
openssl rand -hex 32
# 将输出填入 api-key-secret.yaml 后 apply
kubectl apply -f api-key-secret.yaml
```

### 4.4 配置 imagePullSecret（私有仓库必须）

当前交付包已在 `rbac.yaml` 的三个 ServiceAccount 中预挂载 `opensandbox-image-pull-secret`。如果 Docker Registry 需要认证，只需创建该 Secret：

```bash
kubectl create secret docker-registry opensandbox-image-pull-secret \
  --docker-server=docker-registry.internal.example.com \
  --docker-username=admin \
  --docker-password=<password> \
  -n opensandbox-system
```

> **说明**：
> - 若 Secret 名称与 `opensandbox-image-pull-secret` 不同，需同步修改 `rbac.yaml` 中三个 ServiceAccount 的 `imagePullSecrets.name`。
> - 若镜像仓库为公开仓库，不创建此 Secret 不影响拉取。
> - 如坚持在每个 Deployment 中单独配置 `imagePullSecrets`，可在 `server-deployment.yaml`、`controller-manager-deployment.yaml`、`ingress-gateway-deployment.yaml` 的 `spec.template.spec` 中添加，但不再推荐。

### 4.5 配置 Server ConfigMap

> `batchsandbox-template.yaml` 是模板片段，不是 K8s 资源，**请勿直接 `kubectl apply`**。它通过 `server-config.yaml` ConfigMap 挂载到 Server 容器内。

```yaml
# server-config.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: opensandbox-server-config
  namespace: opensandbox-system
data:
  config.toml: |
    [server]
    host = "0.0.0.0"
    port = 80
    api_key = ""  # 实际由环境变量 OPENSANDBOX_SERVER_API_KEY 覆盖

    [log]
    level = "INFO"

    [runtime]
    type = "kubernetes"
    execd_image = "docker-registry.internal.example.com/opensandbox/execd:v1.0.21"

    [kubernetes]
    kubeconfig_path = ""
    namespace = "opensandbox"
    informer_enabled = true
    informer_resync_seconds = 300
    informer_watch_timeout_seconds = 60
    snapshot_create_timeout_seconds = 900
    workload_provider = "batchsandbox"
    batchsandbox_template_file = "/etc/opensandbox/example.batchsandbox-template.yaml"

    [egress]
    image = "docker-registry.internal.example.com/opensandbox/egress:v1.1.5"
    mode = "dns+nft"

    [ingress]
    mode = "gateway"
    gateway.address = "opensandbox-ingress-gateway.opensandbox-system.svc"
    gateway.route.mode = "header"

  example.batchsandbox-template.yaml: |
    # Example BatchSandbox CR template for OpenSandbox Kubernetes runtime
    metadata:
    spec:
      replicas: 1
      template:
        spec:
          restartPolicy: Never
          tolerations:
            - operator: "Exists"
```

> **注意**：`gateway.address` 不可为空，否则 Server 启动会因校验失败而崩溃。

### 4.6 配置 RBAC

```yaml
# rbac.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: opensandbox-ingress-gateway-reader-full
rules:
- apiGroups:
  - ''
  resources:
  - pods
  - pods/status
  - services
  verbs:
  - get
  - list
  - watch
- apiGroups:
  - sandbox.opensandbox.io
  resources:
  - batchsandboxes
  - batchsandboxes/status
  verbs:
  - get
  - list
  - watch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: opensandbox-manager-role
rules:
- apiGroups:
  - ''
  resources:
  - events
  - pods
  verbs:
  - create
  - delete
  - get
  - list
  - patch
  - update
  - watch
- apiGroups:
  - ''
  resources:
  - pods/status
  verbs:
  - get
  - patch
  - update
- apiGroups:
  - ''
  resources:
  - configmaps
  verbs:
  - get
  - list
  - watch
- apiGroups:
  - ''
  resources:
  - secrets
  verbs:
  - get
  - list
  - watch
- apiGroups:
  - batch
  resources:
  - jobs
  verbs:
  - create
  - delete
  - get
  - list
  - patch
  - update
  - watch
- apiGroups:
  - batch
  resources:
  - jobs/status
  verbs:
  - get
  - patch
  - update
- apiGroups:
  - sandbox.opensandbox.io
  resources:
  - batchsandboxes
  - pools
  - sandboxsnapshots
  verbs:
  - create
  - delete
  - get
  - list
  - patch
  - update
  - watch
- apiGroups:
  - sandbox.opensandbox.io
  resources:
  - batchsandboxes/finalizers
  - pools/finalizers
  - sandboxsnapshots/finalizers
  verbs:
  - update
- apiGroups:
  - sandbox.opensandbox.io
  resources:
  - batchsandboxes/status
  - pools/status
  - sandboxsnapshots/status
  verbs:
  - get
  - patch
  - update
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: opensandbox-server-role
rules:
- apiGroups:
  - ''
  resources:
  - pods
  - pods/status
  - pods/log
  - events
  - services
  - configmaps
  verbs:
  - create
  - delete
  - get
  - list
  - patch
  - update
  - watch
- apiGroups:
  - ''
  resources:
  - secrets
  verbs:
  - create
  - delete
  - get
- apiGroups:
  - ''
  resources:
  - persistentvolumeclaims
  verbs:
  - create
  - delete
  - get
  - list
  - patch
- apiGroups:
  - node.k8s.io
  resources:
  - runtimeclasses
  verbs:
  - get
  - list
- apiGroups:
  - sandbox.opensandbox.io
  resources:
  - batchsandboxes
  - batchsandboxes/status
  - batchsandboxes/finalizers
  verbs:
  - create
  - delete
  - get
  - list
  - patch
  - update
  - watch
- apiGroups:
  - sandbox.opensandbox.io
  resources:
  - pools
  - pools/status
  - pools/finalizers
  verbs:
  - create
  - delete
  - get
  - list
  - patch
  - update
  - watch
- apiGroups:
  - sandbox.opensandbox.io
  resources:
  - sandboxsnapshots
  - sandboxsnapshots/status
  - sandboxsnapshots/finalizers
  verbs:
  - create
  - delete
  - get
  - list
  - patch
  - update
  - watch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: opensandbox-ingress-gateway-reader-full
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: opensandbox-ingress-gateway-reader-full
subjects:
- kind: ServiceAccount
  name: opensandbox-ingress-gateway
  namespace: opensandbox-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: opensandbox-manager-role
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: opensandbox-manager-role
subjects:
- kind: ServiceAccount
  name: opensandbox-controller-manager
  namespace: opensandbox-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: opensandbox-server-rolebinding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: opensandbox-server-role
subjects:
- kind: ServiceAccount
  name: opensandbox-server
  namespace: opensandbox-system
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: opensandbox-controller-manager
  namespace: opensandbox-system
imagePullSecrets:
- name: opensandbox-image-pull-secret
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: opensandbox-ingress-gateway
  namespace: opensandbox-system
imagePullSecrets:
- name: opensandbox-image-pull-secret
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: opensandbox-server
  namespace: opensandbox-system
imagePullSecrets:
- name: opensandbox-image-pull-secret
---
# Controller Manager Leader Election 专用 RBAC
# 当 controller 以 --leader-elect 启动时，需要在 opensandbox-system 命名空间内
# 操作 configmaps 和 leases 资源以完成 leader 选举。
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: opensandbox-leader-election-role
  namespace: opensandbox-system
rules:
- apiGroups:
  - ""
  resources:
  - configmaps
  verbs:
  - create
  - delete
  - get
  - list
  - patch
  - update
  - watch
- apiGroups:
  - coordination.k8s.io
  resources:
  - leases
  verbs:
  - create
  - delete
  - get
  - list
  - patch
  - update
  - watch
- apiGroups:
  - ""
  resources:
  - events
  verbs:
  - create
  - patch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: opensandbox-leader-election-rolebinding
  namespace: opensandbox-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: opensandbox-leader-election-role
subjects:
- kind: ServiceAccount
  name: opensandbox-controller-manager
  namespace: opensandbox-system
```

> **注意**：CRD 的 apiGroup 是 `sandbox.opensandbox.io`，不是 `opensandbox.io`。写错会导致 `cannot list resource \"batchsandboxes\" in API group \"sandbox.opensandbox.io\"` 错误。

### 4.7 Server Deployment + Service

```yaml
# server-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: opensandbox-server
  namespace: opensandbox-system
  labels:
    app.kubernetes.io/name: opensandbox-server
spec:
  replicas: 1  # 生产环境建议 >= 2
  selector:
    matchLabels:
      app.kubernetes.io/name: opensandbox-server
  template:
    metadata:
      labels:
        app.kubernetes.io/name: opensandbox-server
    spec:
      serviceAccountName: opensandbox-server
      containers:
        - name: main
          image: docker-registry.internal.example.com/opensandbox/server:v0.2.2
          args: ["--config", "/etc/opensandbox/config.toml"]
          env:
            - name: SANDBOX_CONFIG_PATH
              value: /etc/opensandbox/config.toml
            - name: OPENSANDBOX_SERVER_API_KEY
              valueFrom:
                secretKeyRef:
                  name: opensandbox-api-key
                  key: api-key
          ports:
            - containerPort: 80
              name: http
          livenessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 10
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: 250m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 512Mi
          volumeMounts:
            - name: config
              mountPath: /etc/opensandbox
      volumes:
        - name: config
          configMap:
            name: opensandbox-server-config
---
apiVersion: v1
kind: Service
metadata:
  name: opensandbox-server
  namespace: opensandbox-system
spec:
  selector:
    app.kubernetes.io/name: opensandbox-server
  ports:
    - port: 80
      targetPort: http
      name: http
```

### 4.8 Controller Manager Deployment

```yaml
# controller-manager-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: opensandbox-controller-manager
  namespace: opensandbox-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/instance: opensandbox
      app.kubernetes.io/name: opensandbox
      control-plane: controller-manager
  template:
    metadata:
      labels:
        app.kubernetes.io/instance: opensandbox
        app.kubernetes.io/name: opensandbox
        control-plane: controller-manager
    spec:
      serviceAccountName: opensandbox-controller-manager
      containers:
        - name: manager
          image: docker-registry.internal.example.com/opensandbox/controller:v0.2.0
          command: ["/workspace/server"]
          args:
            - --leader-elect
            - --health-probe-bind-address=:8081
            - --zap-log-level=info
            - --kube-client-qps=100
            - --kube-client-burst=200
            - --image-committer-image=docker-registry.internal.example.com/opensandbox/image-committer:v0.1.1
            - --commit-job-timeout=10m
          ports:
            - containerPort: 8081
              name: health
          livenessProbe:
            httpGet:
              path: /healthz
              port: health
            initialDelaySeconds: 15
            periodSeconds: 20
          readinessProbe:
            httpGet:
              path: /readyz
              port: health
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: "1"
              memory: 512Mi
```

> **注意**：Controller Manager 是核心组件，没有它 Server 创建 CR 后不会有任何沙箱 Pod。

### 4.9 Ingress Gateway（可选）

仅在直连模式 `use_server_proxy=False` 时需要。

```yaml
# ingress-gateway-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: opensandbox-ingress-gateway
  namespace: opensandbox-system
spec:
  selector:
    app.kubernetes.io/instance: opensandbox
    app.kubernetes.io/name: opensandbox-ingress-gateway
  ports:
    - name: http
      port: 80
      targetPort: 28888
  type: ClusterIP
```

```yaml
# ingress-gateway-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: opensandbox-ingress-gateway
  namespace: opensandbox-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/instance: opensandbox
      app.kubernetes.io/name: opensandbox-ingress-gateway
  template:
    metadata:
      labels:
        app.kubernetes.io/instance: opensandbox
        app.kubernetes.io/name: opensandbox-ingress-gateway
    spec:
      serviceAccountName: opensandbox-ingress-gateway
      containers:
        - name: main
          image: docker-registry.internal.example.com/opensandbox/ingress:v1.0.10
          args:
            - --namespace=opensandbox
            - --port=28888
            - --provider-type=batchsandbox
            - --mode=header
            - --log-level=info
          ports:
            - containerPort: 28888
              name: http
          livenessProbe:
            httpGet:
              path: /status.ok
              port: http
            initialDelaySeconds: 10
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: /status.ok
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: "1"
              memory: 512Mi
```

> **注意**：`ingress-gateway-service.yaml` 使用 `ClusterIP` 类型，由集群自动分配 Service IP，**不要硬编码 `clusterIP`**。

### 4.10 一键安装脚本

```bash
#!/bin/bash
# install.sh
set -e

kubectl apply -f crds.yaml
kubectl apply -f namespace.yaml
kubectl apply -f rbac.yaml
kubectl apply -f api-key-secret.yaml
kubectl apply -f server-config.yaml
kubectl apply -f controller-manager-deployment.yaml
kubectl apply -f ingress-gateway-service.yaml
kubectl apply -f ingress-gateway-deployment.yaml
kubectl apply -f server-deployment.yaml

echo ""
echo "==> 等待组件就绪..."
kubectl rollout status deployment/opensandbox-controller-manager -n opensandbox-system --timeout=180s
kubectl rollout status deployment/opensandbox-server -n opensandbox-system --timeout=180s

echo ""
echo "==> OpenSandbox 部署完成"
```

---

## 5. 网络访问方案

### 5.1 Server Proxy 模式（推荐）

适用场景：客户端无法直连沙箱 Pod 网络（本地开发、跨网段）。

```python
from opensandbox import ConnectionConfig

config = ConnectionConfig(
    domain="opensandbox-server.opensandbox-system.svc:80",
    api_key="YOUR_API_KEY",
    use_server_proxy=True
)
```

优点：无需 Ingress Gateway，配置简单。  
缺点：流量经过 Server，高并发需扩容 Server。

### 5.2 Ingress Gateway 直连模式

适用场景：客户端与 K8s 集群同网段，追求低延迟。

```python
config = ConnectionConfig(
    domain="opensandbox-ingress-gateway.opensandbox-system.svc:80",
    api_key="YOUR_API_KEY",
    use_server_proxy=False
)
```

需要提前部署 Ingress Gateway。

### 5.3 集群外访问方式

ClusterIP 只能在集群内部访问。从集群外访问需：

#### 方式 A：kubectl port-forward

```bash
kubectl port-forward svc/opensandbox-server 9080:80 -n opensandbox-system
```

访问：`http://localhost:9080`

#### 方式 B：NodePort Service

如果 `kubectl port-forward` 因节点环境不可用，可新增 NodePort Service：

```yaml
# server-nodeport-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: opensandbox-server-nodeport
  namespace: opensandbox-system
spec:
  type: NodePort
  selector:
    app.kubernetes.io/name: opensandbox-server
  ports:
    - port: 80
      targetPort: http
      nodePort: 30080
```

应用后通过 `http://<任意节点IP>:30080` 访问。

> 注意：NodePort 不修改原 ClusterIP Service，名称不同避免冲突。

---

## 6. 验证

### 6.1 组件状态检查

```bash
# 查看系统组件
kubectl get pods -n opensandbox-system

# 查看 CRD
kubectl get crd | grep sandbox.opensandbox.io

# 查看 Server 健康
kubectl port-forward svc/opensandbox-server 9080:80 -n opensandbox-system &
curl http://localhost:9080/health
# 预期: {"status":"healthy"}
```

### 6.2 SDK 创建沙箱验证

```bash
export OPENSANDBOX_DOMAIN="opensandbox-server.opensandbox-system.svc:80"
export OPENSANDBOX_API_KEY="YOUR_API_KEY"
export OPENSANDBOX_PROTOCOL="http"
export OPENSANDBOX_USE_SERVER_PROXY="true"
export OPENSANDBOX_IMAGE="docker-registry.internal.example.com/library/python:3.13-slim"
```

```bash
./.venv/bin/python -c "
import asyncio
from opensandbox import AsyncSandbox
from opensandbox import ConnectionConfig

async def main():
    config = ConnectionConfig(
        domain='opensandbox-server.opensandbox-system.svc:80',
        api_key='YOUR_API_KEY',
        use_server_proxy=True
    )
    sbx = await AsyncSandbox.create(config=config)
    print('sandbox id:', sbx.sandbox_id)
    result = await sbx.commands.run('env')
    print(result.stdout)
    await sbx.kill()

asyncio.run(main())
"
```

成功输出环境变量即表示 OpenSandbox 可正常使用。

---

## 7. 运维指南

### 7.1 日常命令

```bash
# 查看系统组件
kubectl get pods -n opensandbox-system

# 查看沙箱 Pod
kubectl get pods -n opensandbox

# 查看沙箱事件
kubectl get events -n opensandbox --sort-by='.lastTimestamp' | tail -20

# 查看 Server 日志
kubectl logs deployment/opensandbox-server -n opensandbox-system -f

# 查看 Controller Manager 日志
kubectl logs deployment/opensandbox-controller-manager -n opensandbox-system -f

# 清理残留沙箱
kubectl delete pods --all -n opensandbox
```

### 7.2 扩缩容

```bash
# Server 扩容
kubectl scale deployment/opensandbox-server --replicas=3 -n opensandbox-system

# Ingress Gateway 扩容（直连模式）
kubectl scale deployment/opensandbox-ingress-gateway --replicas=2 -n opensandbox-system

# 查看节点资源
kubectl describe nodes | grep -A 5 "Allocated resources"
```

### 7.3 故障排查

| 症状 | 可能原因 | 排查命令 |
| --- | --- | --- |
| `cannot list resource "batchsandboxes"` | RBAC apiGroup 写错 | `kubectl describe clusterrole opensandbox-server-role` |
| 创建沙箱 504 超时 | Controller Manager 未运行 | `kubectl get pods -n opensandbox-system` |
| Server CrashLoopBackOff | `gateway.address` 为空 | `kubectl logs deployment/opensandbox-server` |
| ImagePullBackOff | 镜像地址错误或仓库无权限 | `kubectl describe pod <pod> -n opensandbox-system` |
| Pod 无法调度 | 节点资源不足 | `kubectl describe nodes` |
| 沙箱创建后无法连接 | `use_server_proxy` 配置错误 | 确认 ConnectionConfig |

---

## 8. Pool CRD 池模式（可选）

OpenSandbox 支持通过 `Pool` 自定义资源预热沙箱 Pod。创建 `BatchSandbox` 时通过 `poolRef` 引用 Pool，Controller Manager 会直接从预热池中分配 Pod，显著降低首次创建延迟。

### 8.1 适用场景

- 对沙箱创建延迟敏感（要求 <1s 首次响应）
- 多应用共享同一类基础镜像（如 `python:3.13-slim`）
- 愿意以常驻 Pod 资源换取更低延迟

### 8.2 启用步骤

1. 修改 `deploy/opensandbox/pool-example.yaml` 中的镜像地址为内网仓库地址。
2. 应用 Pool CR：

   ```bash
   cd deploy/opensandbox
   kubectl apply -f pool-example.yaml
   ```

3. 等待 Pool 预热完成：

   ```bash
   kubectl get pool -n opensandbox -w
   # 预期 AVAILABLE >= bufferMin
   ```

4. 创建引用 Pool 的 BatchSandbox 进行验证：

   ```bash
   kubectl apply -f batchsandbox-pool-example.yaml
   ```

5. 观察分配结果：

   ```bash
   kubectl get batchsandbox -n opensandbox
   kubectl get pods -n opensandbox
   ```

### 8.3 资源影响

以 `pool-example.yaml` 默认配置为例：

| 配置项 | 值 | 常驻资源 |
| --- | --- | --- |
| poolMax | 10 | 最多 10 个预热 Pod |
| 单 Pod 内存请求 | 128Mi | 约 1.25Gi 内存 |
| 单 Pod CPU 请求 | 100m | 约 1 核 CPU |

请根据实际并发需求调整 `poolMin` / `poolMax` / `bufferMin` / `bufferMax`。

### 8.4 回收策略

| 策略 | 说明 | 适用场景 |
| --- | --- | --- |
| Delete | 删除 Pod，由 Controller 重新创建（默认） | 隔离性要求高 |
| Restart | 重启容器，保留 Pod | 启动速度快 |
| Noop | 不做处理 | 特殊调试场景 |

### 8.5 与 Server 集成说明

当前交付包中 `server-config.yaml` 的 `workload_provider = "batchsandbox"`，Server 默认使用 `template` 创建 BatchSandbox。如需通过 SDK/Server 自动使用 Pool，业务侧需在创建 BatchSandbox 时显式指定 `poolRef`，或联系 OpenSandbox 提供方确认 Server 是否支持自动 pool 路由。

---

## 9. 资源规划

### 8.1 按并发用户数估算

| 并发用户数 | 单沙箱内存 | 节点总内存需求 | 建议节点配置 |
| --- | --- | --- | --- |
| 10 | 128Mi | ~2Gi | 4Gi x 1 |
| 50 | 128Mi | ~8Gi | 8Gi x 2 |
| 100 | 256Mi | ~30Gi | 16Gi x 3 |
| 50 (Code Interpreter) | 512Mi | ~30Gi | 16Gi x 3 |

### 8.2 ResourceQuota

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: opensandbox-quota
  namespace: opensandbox
spec:
  hard:
    pods: "100"
    requests.cpu: "10"
    requests.memory: 20Gi
    limits.cpu: "20"
    limits.memory: 40Gi
```

### 8.3 LimitRange

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: opensandbox-limits
  namespace: opensandbox
spec:
  limits:
    - type: Container
      default:
        cpu: 500m
        memory: 256Mi
      defaultRequest:
        cpu: 100m
        memory: 128Mi
      max:
        cpu: "2"
        memory: 2Gi
```

---

## 10. 附录：v1.0 到 v2.0 关键变更

| 变更项 | v1.0 | v2.0 |
| --- | --- | --- |
| 镜像同步 | 缺少 controller、ingress、image-committer | 完整清单 |
| CRDs | 未提及 | 必须安装 |
| Controller Manager | 未部署 | 必须部署 |
| RBAC apiGroup | 错误 `opensandbox.io` | 正确 `sandbox.opensandbox.io` |
| BatchSandbox 模板 | 未提供 | 嵌入 ConfigMap |
| `gateway.address` | 空字符串 | 设置为 Service DNS |
| `clusterIP` | 可能硬编码 | 自动分配 |
| imagePullSecret | 未提及 | 在 ServiceAccount 中预挂载 `opensandbox-image-pull-secret`，私有仓库只需创建 Secret |
| Controller Leader Election RBAC | 未配置 | 新增 Role/RoleBinding，支持 `--leader-elect` 高可用 |
| 一键安装脚本 | 无 | 提供 install.sh |
| 离线 Python 依赖 | 未提及 | 需单独准备（见第 11 章） |
| Pool CRD 池模式 | 未提及 | 提供 Pool CR 示例与使用说明 |

---

## 11. 离线 Python 依赖（可选）

如果客户运行环境无法连接公网安装 Python 包，需提前准备离线依赖。

### 11.1 在外网环境打包

```bash
pip download opensandbox==0.1.15 -d ./offline-packages/
pip download pytest pytest-asyncio pytest-anyio httpx pydantic -d ./offline-packages/
```

### 11.2 内网安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --no-index --find-links ./offline-packages opensandbox==0.1.15
```

---

## 12. 交付清单

```text
opensandbox-delivery/
├── crds.yaml
├── namespace.yaml
├── rbac.yaml
├── api-key-secret.yaml
├── server-config.yaml
├── server-deployment.yaml
├── controller-manager-deployment.yaml
├── ingress-gateway-service.yaml
├── ingress-gateway-deployment.yaml
├── server-nodeport-service.yaml
├── image-pull-secret.yaml
├── pool-example.yaml
├── batchsandbox-pool-example.yaml
├── sync_opensandbox_images.sh
├── install.sh
├── requirements.txt
├── OpenSandbox 集群部署手册 v2.0.md
└── OpenSandbox 客户部署操作手册.md
```
