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

# ---------- 非 root 运行（台账 E2）----------
# 容器被攻破时不该直接拿到 root：默认建一个普通用户，最后一律以它运行。
# uid/gid 默认 1000，与常见 Linux 宿主用户一致 —— `./data`、`./config` 这类
# bind mount 的属主通常就是 1000，uid 不一致时非 root 进程写不进挂载目录
# （Windows/Docker Desktop 的 9p 挂载不校验属主，本地开发不受影响）。
# 需要自定义时：`docker compose build --build-arg APP_UID=$(id -u)`。
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} appgroup \
    && useradd -m -u ${APP_UID} -g ${APP_GID} -s /usr/sbin/nologin appuser

# 依赖安装：torch/torchvision 走 CPU 源，避免拉到 CUDA 版（镜像会大 2~3 倍）。
#
# 这里用 --no-deps 是**构建提速**的关键：
#   --index-url 会把 torch 的**全部依赖**也导向 pytorch 索引，而该索引在本网络
#   实测只有 ~30KB/s（numpy 16.7MB 单独就花了约 9 分钟，整个 pip 阶段 21.6 分钟）。
#   加 --no-deps 后本步只下 torch/torchvision 本体（实测 3.2MB/s，约 80 秒），
#   它们的依赖改由下一步从清华源补齐 —— requirements.txt 里的 torch>=2.0.0/
#   torchvision>=0.15.0 已被满足，但 pip 仍会解析其 Requires-Dist 并装上缺失的依赖。
#   实测依赖完整（torch/ultralytics/cv2 均可正常 import）。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYTORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
COPY requirements.txt ./
RUN pip install --no-deps --index-url ${PYTORCH_CPU_INDEX} torch torchvision \
    && pip install --index-url ${PIP_INDEX_URL} -r requirements.txt

# 应用代码
COPY . .

# 前端产物来自阶段 1
COPY --from=frontend /build/dist ./frontend-vue/dist

# 运行时目录（模型与 data 通过 compose 挂载）
RUN mkdir -p /app/data

# Ultralytics 会把自己的子目录名再拼到 YOLO_CONFIG_DIR 之后
# （即实际用 /tmp/Ultralytics/Ultralytics），且判定依据是
# **父目录是否存在且可写**。只设变量不建目录，它会报
# "user config directory '.../Ultralytics' is not writable" —— 所以必须一并 mkdir。
# 放在最后：避免使上方那层昂贵的 pip 安装层缓存失效。
RUN mkdir -p /tmp/Ultralytics
ENV YOLO_CONFIG_DIR=/tmp/Ultralytics

# 非 root 前必须把**运行期要写**的路径交给 appuser，否则这些功能会静默失效
# （它们都只打一行日志，看起来像"配置没生效"，很难定位）：
#   - /app/config/*.yaml   ROI 保存（`roi_manager.save_to_yaml`）、摄像头配置持久化
#   - /app/data            SQLite 鉴权库 / 上传视频 / 录制文件（compose 里是 bind mount，
#                          Linux 下要求宿主目录属主与 APP_UID 一致）
#   - /app/qdrant_data     仅嵌入式向量模式用（compose 走 Server 模式不需要，留空目录兜底）
#   - /tmp/Ultralytics     ultralytics 配置目录（判定依据就是"父目录可写"）
#
# ⚠ **只 chown 这几个目录，不要 `chown -R /app`**：那会把代码目录也变成可写，
# 被攻破的进程就能改自己的代码（实测踩过：`/app/api` 变成可写）。
RUN mkdir -p /app/qdrant_data \
    && chown -R appuser:appgroup /app/config /app/data /app/qdrant_data /tmp/Ultralytics

# 以非 root 运行（此后所有 RUN/CMD/HEALTHCHECK 都在该用户下）
USER appuser

EXPOSE 8000

# 健康检查：用 **readiness** 端点而非 liveness。
# /api/health 只证明进程活着；本项目实际发生过"MySQL 口令没传进容器 → 后端静默
# 回退 SQLite → /api/health 依然 200 healthy → 编排与看板全以为正常，但业务数据
# 一条都读不到"。改用 /api/health/ready 后，MySQL 不可用会返回 503 → 容器变
# `unhealthy`，问题立刻可见（Qdrant/Redis 等非致命依赖只在响应体里报 degraded，
# 不会因此判死）。
# start-period 给足：模型预热 + 向量索引后台重建需要时间。
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health/ready || exit 1

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
