# ============================================================
# 边缘网关镜像（Go，多阶段构建）
#
# 设计说明：
#   1. 前端在镜像内构建：与主 Dockerfile 用**同一个 node 基础镜像 digest
#      和同一组构建步骤**，保证 edge 托管的前端产物与 Python 版本逐字一致。
#   2. 纯 Go 静态二进制（CGO_ENABLED=0）：零第三方依赖（仅标准库），
#      构建期不需要网络，运行时可放进 alpine 最小镜像。
#   3. 运行镜像里的 dist 是**镜像内容**而非挂载：服务器无 GitHub、
#      bind mount 有静默失效历史，产物进镜像最稳。
# ============================================================

# ---------- 阶段 1：构建前端（钉 digest，与主 Dockerfile 保持一致） ----------
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS frontend
WORKDIR /build
# 先只拷依赖清单，命中 Docker 层缓存（源码变更不会重装依赖）
COPY frontend-vue/package.json frontend-vue/package-lock.json ./
RUN npm ci --no-audit --no-fund --registry=https://registry.npmmirror.com
COPY frontend-vue/ ./
RUN npm run build

# ---------- 阶段 2：构建 edge 二进制 ----------
# digest 取自本机实际拉取的那一个（docker pull golang:1.24-alpine）。
FROM golang:1.24-alpine@sha256:8bee1901f1e530bfb4a7850aa7a479d17ae3a18beb6e09064ed54cfd245b7191 AS build
WORKDIR /src
# 先拷依赖清单（当前零第三方依赖、无 go.sum；保留此模式以便将来加依赖时
# 源码变更不会使依赖解析层缓存失效）
COPY edge/go.mod ./
COPY edge/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/edge ./cmd/edge

# ---------- 阶段 3：运行时（最小镜像） ----------
# digest 同取自本机实际拉取（docker pull alpine:3.20）。
FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc AS runtime

# 前端产物 + 二进制；dist 路径与 EDGE_DIST_DIR 对应
COPY --from=frontend /build/dist /srv/dist
COPY --from=build /out/edge /usr/local/bin/edge

# 默认值面向 compose 内网；对外端口/后端地址可由 compose override 覆盖
ENV EDGE_LISTEN=:8000 \
    EDGE_BACKEND_URL=http://backend:8000 \
    EDGE_DIST_DIR=/srv/dist

EXPOSE 8000

# 非 root 运行：alpine 自带 nobody（uid 65534），静态托管只读、无需写权限
USER nobody

# 健康检查用 **liveness**（本地直接应答）而非 readiness：
# edge 的职责含"后端挂了也能继续供静态页/健康探针"，故后端不可用时
# edge 自身不应被判死（后端就绪由 backend 容器自己的 HEALTHCHECK 负责）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["/usr/local/bin/edge"]
