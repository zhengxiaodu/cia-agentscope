# ---- 构建阶段 ----
FROM python:3.13-slim AS builder

WORKDIR /build

# 先拷贝依赖文件，利用 Docker 缓存层
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -t /build/deps

# ---- 运行阶段 ----
FROM python:3.13-slim

LABEL maintainer="zhengxiaodu"

# 安装运行时系统依赖（Docker 客户端等）
RUN apt-get update && \
    apt-get install -y --no-install-recommends docker.io && \
    rm -rf /var/lib/apt/lists/*

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

# 暴露端口
EXPOSE 7010

# .env 通过 --env-file 或 volume 挂载注入，不打入镜像
# 启动命令
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7010"]
