// Command edge 是智能零售系统的 Go 边缘网关（迁移阶段 S1）。
//
// 职责（全部无业务逻辑）：
//
//	静态托管（不经过 Python）· 匿名 liveness · 前端加载失败上报 ·
//	内网 /metrics · 其余一切兜底反代（21 个兼容别名零枚举覆盖）。
//
// 不碰像素；默认也不持数据库句柄。唯一的例外是 GO_OWNS_INGEST_WRITES=1 时，
// 由 internal/store 持有三张视频派生表（retail_stats / alert_record /
// track_visit_paths）的写句柄 —— 见 改进记录.md / 项目部分go语言重构.md。
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"retail-assistant/edge/internal/auth"
	"retail-assistant/edge/internal/config"
	"retail-assistant/edge/internal/health"
	"retail-assistant/edge/internal/httpx"
	"retail-assistant/edge/internal/metrics"
	"retail-assistant/edge/internal/proxy"
	"retail-assistant/edge/internal/report"
	"retail-assistant/edge/internal/routing"
	"retail-assistant/edge/internal/static"
	"retail-assistant/edge/internal/store"
	"retail-assistant/edge/internal/video"
)

type app struct {
	m    *metrics.Metrics
	st   *static.Handler
	px   *proxy.Proxy
	gate *auth.Gate
	vid  *video.Handler  // nil = 视频数据面未启用（WS 照旧反代）
	rep  *report.Handler // nil = 汇报扇出未启用（WS 照旧反代）
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

	// 汇报广播入口（Python → edge）：只在本进程启用时处理，其余 /internal/**
	// 仍落到下面的 switch 返回本地 404。
	if a.rep != nil {
		if a.rep.HandleInternal(rw, r) {
			return // kind 已是 Internal（routing.Classify 的归类）
		}
	}

	// 汇报订阅 WS（/api/ws/reports 与兼容别名）：归类之前先问扇出层。
	// 与视频一样只认真正的升级请求；不接管就落回 switch 走兜底反代。
	if a.rep != nil {
		if k, handled := a.rep.Handle(rw, r); handled {
			kind = k
			return
		}
	}

	// 视频 WS（/api/ws/stream、/api/ws/client）：归类之前先问数据面。
	// 它只认真正的升级请求；不接管（没开、拿不准结论、正在关闭）就返回 false，
	// 请求原样落到下面的 switch —— 仍是 Proxy，走兜底反代。
	if a.vid != nil {
		if k, handled := a.vid.Handle(rw, r); handled {
			kind = k
			return
		}
	}

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
	case routing.Internal:
		// Python 的内部端点路径：公网请求到此为止，不出网（Python 侧另有独立门禁）。
		auth.ServeInternalNotFound(rw, r)
	default:
		if dec, reason := a.gate.Inspect(r); dec == auth.Deny {
			a.m.IncAuthDenied()
			slog.Info("auth denied",
				"path", r.URL.Path, "reason", reason,
				"ip", httpx.ClientIP(r.RemoteAddr))
			auth.ServeDenied(rw, r)
			return
		}
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
	// 边缘鉴权早拒（可选，EDGE_AUTH=1）。缺密钥即禁用：宁可退回"全部反代"，
	// 也不能让 edge 在拿不到权威结论时自行其是。
	gateEnabled := cfg.EdgeAuth && cfg.InternalToken != ""
	if cfg.EdgeAuth && !gateEnabled {
		slog.Warn("EDGE_AUTH=1 但 INTERNAL_API_TOKEN 为空：鉴权早拒已禁用，全部请求照旧反代")
	}
	verifier := auth.NewVerifier(cfg.BackendURL, cfg.InternalToken, auth.Observer{
		OnVerify:      m.IncVerify,
		OnCacheHit:    m.IncCacheHit,
		OnVerifyError: m.IncVerifyError,
	})
	a := &app{m: m, st: static.New(cfg.DistDir), px: px,
		gate: auth.NewGate(verifier, gateEnabled)}

	// 主动汇报 WS 扇出（S4，可选，EDGE_REPORT=1）。汇报消息由 Python 生成，
	// edge 只负责连接与扇出；Python 经 /internal/report/broadcast 投递。
	rep := report.New(report.Options{
		Enabled: cfg.EdgeReport, InternalToken: cfg.InternalToken,
	})
	if rep.Enabled() {
		a.rep = rep
	} else if cfg.EdgeReport {
		slog.Warn("EDGE_REPORT=1 但 INTERNAL_API_TOKEN 为空：" +
			"汇报扇出未接管，/api/ws/reports 照旧反代给 Python")
	}
	m.SetReportSource(rep.MetricsSource()) // 未启用也挂上：/metrics 能看出"没在接管"

	// 入库写者（S4，可选，GO_OWNS_INGEST_WRITES=1）。前提是视频数据面已接管：
	// 入库事件只走那条上行流；没有它，Python 侧也不会再直写这三张表。
	var ingestStore *store.Store
	var ingestSink video.IngestSink
	if cfg.GoOwnsIngest {
		if !cfg.EdgeVideo || !gateEnabled {
			slog.Error("GO_OWNS_INGEST_WRITES=1 但视频数据面未启用" +
				"（需 EDGE_VIDEO=1、EDGE_AUTH=1、INTERNAL_API_TOKEN 非空）：" +
				"retail_stats / alert_record / track_visit_paths 将不会写入")
		} else if st, err := store.Open(store.Config{
			Host: cfg.MySQLHost, Port: cfg.MySQLPort, User: cfg.MySQLUser,
			Password: cfg.MySQLPassword, Database: cfg.MySQLDatabase,
		}); err != nil {
			slog.Error("入库存储初始化失败，三张视频派生表将不会写入", "err", err)
		} else {
			ingestStore, ingestSink = st, st
			go st.Run(context.Background())
			m.SetIngestSource(st.MetricsSource())
		}
	}

	// 视频数据面（S3，可选，EDGE_VIDEO=1）。与早拒共用同一个判定器：
	// 同一份 TTL 缓存，"这条连接能不能算登录了"两侧答案一致。
	vid := video.New(video.Options{
		Video: cfg.EdgeVideo, AuthEnabled: cfg.EdgeAuth,
		BackendURL: cfg.BackendURL, InternalToken: cfg.InternalToken,
		Verifier: verifier, Ingest: ingestSink,
	})
	if vid.Enabled() {
		a.vid = vid
		vid.Start()
	} else if cfg.EdgeVideo {
		slog.Warn("EDGE_VIDEO=1 但 EDGE_AUTH=0 或 INTERNAL_API_TOKEN 为空：" +
			"视频数据面未接管，/api/ws/** 照旧反代给 Python")
	}
	m.SetVideoSource(vid.MetricsSource()) // 未启用也挂上：/metrics 里能看出"没在接管"

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

	// 汇报与视频连接**必须在 Shutdown 之前**收掉：WS 握手时连接已被 Hijack，
	// 不在 http.Server 的管理范围内 —— Shutdown 既不等它们，也关不掉它们。
	if a.rep != nil {
		a.rep.Close()
	}
	if a.vid != nil {
		a.vid.Close()
	}
	// 上行流已停 → 不会再有入库事件；把已收到的批次刷完再退出。
	if ingestStore != nil {
		ingestStore.Close()
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		slog.Warn("优雅关闭超时，强制关闭", "err", err)
		_ = srv.Close()
	}
	slog.Info("edge 已退出")
}
