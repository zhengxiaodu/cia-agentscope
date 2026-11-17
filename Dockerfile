ARG PYTHON_BASE_IMAGE=registry.ccdc.cn:5000/python:3.13-slim

# ---- 构建阶段：仅使用随项目分发的离线 wheel ----
FROM ${PYTHON_BASE_IMAGE} AS builder

WORKDIR /build

COPY offline-bundle/manifests/requirements-resolved.txt ./requirements-resolved.txt
COPY offline-bundle/python/wheels/ /opt/offline-wheels/

RUN python -m pip install \
        --no-cache-dir \
        --no-index \
        --find-links=/opt/offline-wheels \
        --requirement requirements-resolved.txt \
        --target /build/deps

# ---- 运行阶段 ----
FROM ${PYTHON_BASE_IMAGE}

LABEL maintainer="zhengxiaodu"

# Debian 13 (trixie) / amd64 的 docker.io、docker-cli 及递归运行依赖。
# dpkg 一次性解包并配置全部本地文件，构建期间不会访问 Debian 软件源。
COPY offline-bundle/apt/debs/ /opt/offline-debs/
RUN DEBIAN_FRONTEND=noninteractive dpkg -i /opt/offline-debs/*.deb && \
    rm -rf /opt/offline-debs /var/lib/apt/lists/*

WORKDIR /app

# 拷贝 Python 依赖
COPY --from=builder /build/deps /usr/local/lib/python3.13/site-packages

# 拷贝应用代码
COPY app/ ./app/
COPY config/ ./config/
COPY skills/ ./skills/
COPY tools/ ./tools/
COPY scripts/ ./scripts/

# 创建工作区目录
RUN mkdir -p /data/docker-workspaces

EXPOSE 7010

# .env 通过 --env-file 或 volume 挂载注入，不打入镜像
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7010"]
