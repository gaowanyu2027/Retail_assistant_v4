// Command edge 是智能零售系统的 Go 边缘网关（迁移阶段 S1）。
//
// 职责（全部无业务逻辑）：
//
//	静态托管（不经过 Python）· 匿名 liveness · 前端加载失败上报 ·
//	内网 /metrics · 其余一切兜底反代（21 个兼容别名零枚举覆盖）。
//
// 不碰像素、不持数据库句柄——见 改进记录.md / 项目部分go语言重构.md 的边界约定。
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"retail-assistant/edge/internal/config"
	"retail-assistant/edge/internal/health"
	"retail-assistant/edge/internal/httpx"
	"retail-assistant/edge/internal/metrics"
	"retail-assistant/edge/internal/proxy"
	"retail-assistant/edge/internal/routing"
	"retail-assistant/edge/internal/static"
)

type app struct {
	m  *metrics.Metrics
	st *static.Handler
	px *proxy.Proxy
}

// ServeHTTP 是唯一的请求入口：先归类（决定本地处理还是反代），
// 再统一包一层 RespWriter（安全头 + 状态码记录）。
func (a *app) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	kind := routing.Classify(r.URL.Path)
	rw := httpx.NewRespWriter(w)
	start := time.Now()
	a.m.IncInflight()
	defer func() {
		a.m.DecInflight()
		a.m.IncKind(kind)
		a.m.IncStatus(rw.Status())
		slog.Info("req",
			"method", r.Method,
			"path", r.URL.Path,
			"kind", kind.String(),
			"status", rw.Status(),
			"dur_ms", time.Since(start).Milliseconds(),
			"ip", httpx.ClientIP(r.RemoteAddr),
		)
	}()

	switch kind {
	case routing.Index:
		a.st.ServeIndex(rw, r)
	case routing.Static:
		a.st.ServeStatic(rw, r)
	case routing.Health:
		// 带凭据 → 反代拿完整信息；匿名 → 本地最小应答（Python 挂了也答）。
		if health.HasCredentials(r) {
			a.px.ServeHTTP(rw, r)
		} else {
			health.ServeLiveness(rw, r)
		}
	case routing.Fallback:
		a.serveFallback(rw, r)
	case routing.Metrics:
		a.m.Serve(rw, r)
	default:
		a.px.ServeHTTP(rw, r)
	}
}

// serveFallback 前端加载失败上报：终端记录原因后返回固定应答。
// 本地处理（而不是转给 Python）：前端资源出问题时往往后端也不健康，
// 这条上报恰恰要在那种时刻还能送达并留下记录。
func (a *app) serveFallback(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		httpx.WriteString(w, r, http.StatusMethodNotAllowed, "application/json",
			`{"detail":"Method Not Allowed"}`)
		return
	}
	reason := r.URL.Query().Get("reason")
	if reason == "" {
		reason = "未知原因"
	}
	slog.Warn("[Frontend] Vue 前端加载失败，客户端已自动回退为原生 JS 版。原因: " + reason)
	httpx.WriteString(w, r, http.StatusOK, "application/json",
		`{"status":"ok","fallback":"native"}`)
}

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

	cfg := config.Load()
	m := metrics.New(cfg.BackendURL)
	px, err := proxy.New(cfg.BackendURL, m.IncProxyError)
	if err != nil {
		slog.Error("edge 配置错误", "err", err)
		os.Exit(1)
	}
	a := &app{m: m, st: static.New(cfg.DistDir), px: px}

	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           a,
		ReadHeaderTimeout: 15 * time.Second,
		IdleTimeout:       120 * time.Second,
		// 刻意不设 ReadTimeout / WriteTimeout：视频 WS 与 SSE 是长连接，
		// 固定写超时会把它们拦腰掐断；超时语义在各自协议层内已有。
	}

	slog.Info("edge 启动", "config", cfg.String())
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	select {
	case err := <-errc:
		slog.Error("HTTP 服务退出", "err", err)
		os.Exit(1)
	case sig := <-stop:
		slog.Info("收到退出信号，开始优雅关闭", "signal", sig.String())
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		slog.Warn("优雅关闭超时，强制关闭", "err", err)
		_ = srv.Close()
	}
	slog.Info("edge 已退出")
}
