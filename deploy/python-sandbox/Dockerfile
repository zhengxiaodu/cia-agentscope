# 沙箱专用 Python 镜像：python:3.13-slim + 7 个技能的离线运行依赖 + CJK 字体
#
# 构建上下文布局（本文件所在目录）：
#   ./Dockerfile
#   ./wheels/                                  # 全部 .whl + sandbox-requirements-pinned.txt
#   ./fonts/fonts-noto-cjk_*.deb               # CJK 字体 deb（matplotlib 出图中文用）
#   （若 wheels 是散放的同目录文件而非子目录，把下面 COPY 改为 COPY . /tmp/offline-wheels/）
#
# 构建：docker build -t registry.ccdc.cn:5000/python:3.13-slim-skills .
# 推送：docker push registry.cccd.cn:5000/python:3.13-slim-skills
# 启用：cia-agentscope 的 .env 中设置 OPENSANDBOX_IMAGE=registry.cccd.cn:5000/python:3.13-slim-skills 并重启应用

ARG PYTHON_BASE_IMAGE=registry.ccdc.cn:5000/python:3.13-slim

FROM ${PYTHON_BASE_IMAGE}

# ---- CJK 字体（apt 包，离线 deb 安装，不走 pip）----
# 来源：packages.debian.org 下载 fonts-noto-cjk 的 _all.deb（无其他依赖）
# smart-charts / academic-figures 出图中文标签必需；缺失时中文渲染为方块
# 无需 fc-cache：slim 镜像无 fontconfig，matplotlib 自行扫描 /usr/share/fonts
COPY fonts/ /tmp/fonts/
RUN dpkg -i /tmp/fonts/*.deb && rm -rf /tmp/fonts

COPY wheels/ /tmp/offline-wheels/

# --no-index：完全离线安装，构建机无需外网；装完删除 wheel 控制镜像体积
RUN python -m pip install \
        --no-cache-dir \
        --no-index \
        --find-links=/tmp/offline-wheels \
        --requirement /tmp/offline-wheels/sandbox-requirements-pinned.txt \
    && rm -rf /tmp/offline-wheels

# 构建期冒烟验证 1：核心 import 全部通过才算构建成功（pymupdf 的 import 名即 pymupdf）
RUN python -c "import pandas, numpy, matplotlib, scipy, pymupdf, docx, openpyxl, xlrd, yaml; print('sandbox deps OK')"

# 构建期冒烟验证 2：matplotlib 字体表里必须能找到 Noto Sans CJK，否则构建失败
RUN MPLCONFIGDIR=/tmp/matplotlib python -c "\
from matplotlib import font_manager as fm; \
cjk = [f for f in fm.fontManager.ttflist if 'Noto Sans CJK' in f.name]; \
assert cjk, 'Noto Sans CJK not found'; \
print('CJK font OK:', cjk[0].name)"

# matplotlib 字体/配置缓存指向可写目录，避免沙箱只读文件系统下首次出图失败
ENV MPLCONFIGDIR=/tmp/matplotlib
