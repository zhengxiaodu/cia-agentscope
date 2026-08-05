# OpenSandbox 集群部署

## 前置条件

- K8s 集群 >= 1.24
- kubectl 已配置集群访问
- 节点可用内存 >= 4Gi

## 部署顺序

```bash
# 1. 创建命名空间
kubectl apply -f namespace.yaml

# 2. 创建 API Key Secret（先编辑填入真实密钥）
#    生成: openssl rand -hex 32
kubectl apply -f api-key-secret.yaml

# 3. 部署 Server 配置
kubectl apply -f server-config.yaml

# 4. 创建 RBAC
kubectl apply -f rbac.yaml

# 5. 部署 Server + Service
kubectl apply -f server-deployment.yaml

# 6. (可选) 资源配额
kubectl apply -f resource-quota.yaml

# 7. 等待就绪
kubectl rollout status deployment/opensandbox-server -n opensandbox-system --timeout=120s

# 8. 验证
kubectl port-forward svc/opensandbox-server 9080:80 -n opensandbox-system &
curl http://localhost:9080/health
# 预期: {"status":"healthy"}
```

## 内网镜像同步

如需将镜像同步到内网 Harbor：

```bash
./sync_opensandbox_images.sh harbor.internal.example.com
```

同步后修改 `server-config.yaml` 中的镜像地址为内网 Harbor 地址。

## 文件说明

| 文件 | 用途 |
|---|---|
| namespace.yaml | 创建 opensandbox-system 和 opensandbox 命名空间 |
| api-key-secret.yaml | API 鉴权密钥 |
| server-config.yaml | Server 配置文件（config.toml） |
| rbac.yaml | ServiceAccount + ClusterRole + Binding |
| server-deployment.yaml | Server Deployment + Service |
| resource-quota.yaml | 命名空间 ResourceQuota + LimitRange |
| sync_opensandbox_images.sh | 镜像同步脚本 |
