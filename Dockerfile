# syntax=docker/dockerfile:1
# ============================================================
# 智能零售分析系统 v4 — 后端镜像（多阶段构建）
#
# 设计说明：
#   1. 前端在镜像内构建：`frontend-vue/dist` 被 .gitignore 忽略，
#      别人 clone 后没有前端产物。多阶段构建让 `docker build` 一步到位，
#      使用者无需在宿主机安装 Node。
#   2. 依赖模型/数据用**挂载**提供，不打进镜像（模型文件体积大，
#      且"模型不进镜像"是更规范的做法）。
#   3. torch/torchvision 走 **CPU 源**：默认 PyPI 源会装 CUDA 版，
#      镜像会大 2~3 倍。CV 推理与摄像头采集本就建议留在边缘/本地，
#      容器内只跑业务 API + Agent + 数据侧。
# ============================================================

# ---------- 阶段 1：构建前端（Vue3 + Vite） ----------
FROM node:22-alpine AS frontend
WORKDIR /build
# 先只拷依赖清单，命中 Docker 层缓存（源码变更不会重装依赖）
COPY frontend-vue/package.json frontend-vue/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend-vue/ ./
RUN npm run build

# ---------- 阶段 2：Python 运行时 ----------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# OpenCV / 视频处理在 slim 基础镜像下需要的系统库。
#
# 注意这里**故意不装 ffmpeg**：本项目全代码（含 Python/前端）没有任何
# subprocess/os.system 调用，不存在"调用 ffmpeg 命令行"的路径——
# 视频解码由 opencv-python 自带（wheel 内已捆绑 ffmpeg），
# 音频由 sherpa-onnx 的 Python 绑定直接用 wav 处理。
# 一旦装上 ffmpeg，会连锁拉入 140+ 个编解码依赖包（含 libflite1/libcodec2
# 等大包），使系统层从 ~15 个包膨胀到 ~160 个，镜像增大约 200MB 且构建
# 时间多出十几分钟，属于纯累赘。若将来确实要调 CLI（如转码 mp3），
# 再单独加回来。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖安装：torch/torchvision 先按 CPU 源装好，
# 随后 pip 解析 requirements.txt 时会认为其已满足，不会重复拉 CUDA 版
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYTORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
COPY requirements.txt ./
RUN pip install --index-url ${PYTORCH_CPU_INDEX} torch torchvision \
    && pip install --index-url ${PIP_INDEX_URL} -r requirements.txt

# 应用代码
COPY . .

# 前端产物来自阶段 1
COPY --from=frontend /build/dist ./frontend-vue/dist

# 运行时目录（模型与 data 通过 compose 挂载）
RUN mkdir -p /app/data

EXPOSE 8000

# 健康检查：/api/health 为公开端点（无需登录），适合探活
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
