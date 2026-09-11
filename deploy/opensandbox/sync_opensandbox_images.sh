#!/bin/bash
# sync_opensandbox_images.sh - 将 OpenSandbox 镜像同步到内网 Harbor
# 用法: ./sync_opensandbox_images.sh <harbor-registry>
# 示例: ./sync_opensandbox_images.sh harbor.internal.example.com

set -e

HARBOR_REGISTRY="${1:?用法: $0 <harbor-registry>}"
SOURCE_REGISTRY="sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com"
PROJECT="opensandbox"

IMAGES=(
    "server:v0.2.2"
    "execd:v1.0.21"
    "egress:v1.1.5"
    "code-interpreter:v1.1.0"
)

echo "==> 源镜像仓库: ${SOURCE_REGISTRY}"
echo "==> 目标 Harbor: ${HARBOR_REGISTRY}"
echo ""

for img in "${IMAGES[@]}"; do
    echo "==> 同步 ${img}"
    docker pull "${SOURCE_REGISTRY}/${PROJECT}/${img}"
    docker tag "${SOURCE_REGISTRY}/${PROJECT}/${img}" "${HARBOR_REGISTRY}/${PROJECT}/${img}"
    docker push "${HARBOR_REGISTRY}/${PROJECT}/${img}"
done

# 同步 python 基础镜像
echo "==> 同步 python:3.13-slim"
docker pull python:3.13-slim
docker tag python:3.13-slim "${HARBOR_REGISTRY}/library/python:3.13-slim"
docker push "${HARBOR_REGISTRY}/library/python:3.13-slim"

echo ""
echo "==> 全部同步完成"
echo ""
echo "部署时请将 server-config.yaml 中的镜像地址替换为内网 Harbor 地址"
