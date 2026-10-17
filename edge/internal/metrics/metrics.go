// Package metrics 汇总 edge 自身的运行指标，并只在**内网**暴露 /metrics。
//
// 刻意不引入 Prometheus/Grafana（迁移方案明确不做）：输出 JSON，curl 或
// 看板按需读取即可。访问门禁按直连对端 IP 判定：环回/私网/链路本地可读，
// 其余一律 404（对外不暴露该路径存在）。
package metrics

import (
	"encoding/json"
	"math"
	"net/http"
	"runtime"
	"sync/atomic"
	"time"

	"retail-assistant/edge/internal/httpx"
	"retail-assistant/edge/internal/routing"
)

// Metrics 全部为原子计数，可在任意 goroutine 并发调用。
type Metrics struct {
	start      time.Time
	backendURL string

	kinds       map[routing.Kind]*atomic.Int64
	inflight    atomic.Int64
	proxyErrors atomic.Int64
	s1xx        atomic.Int64
	s2xx        atomic.Int64
	s3xx        atomic.Int64
	s4xx        atomic.Int64
	s5xx        atomic.Int64

	// 边缘鉴权早拒（EDGE_AUTH=1 时才有非零值）
	authDenied atomic.Int64
	verifyOK   atomic.Int64
	verifyErr  atomic.Int64
	cacheHits  atomic.Int64

	// 视频数据面快照（S3 接线；nil = 未接线，输出 null）
	videoSource func() any
	// 入库队列快照（S4；nil = Go 不持有写入权，输出 null）
	ingestSource func() any
	// 主动汇报 WS 扇出快照（S4；nil = 未接线，输出 null）
	reportSource func() any
}

// SetVideoSource 挂上视频数据面的快照函数（main 启动时调用一次）。
func (m *Metrics) SetVideoSource(fn func() any) { m.videoSource = fn }

// SetIngestSource 挂上入库队列的快照函数（main 启动时调用一次）。
func (m *Metrics) SetIngestSource(fn func() any) { m.ingestSource = fn }

// SetReportSource 挂上主动汇报 WS 扇出的快照函数（main 启动时调用一次）。
func (m *Metrics) SetReportSource(fn func() any) { m.reportSource = fn }

func New(backendURL string) *Metrics {
	m := &Metrics{start: time.Now(), backendURL: backendURL,
		kinds: make(map[routing.Kind]*atomic.Int64)}
	for _, k := range routing.AllKinds {
		m.kinds[k] = &atomic.Int64{}
	}
	return m
}

// IncKind 按处理方式计数（proxy/index/static/health/fallback/metrics）。
func (m *Metrics) IncKind(k routing.Kind) {
	if c, ok := m.kinds[k]; ok {
		c.Add(1)
	}
}

// IncStatus 按状态码大类计数。
func (m *Metrics) IncStatus(code int) {
	switch {
	case code >= 100 && code < 200:
		m.s1xx.Add(1)
	case code < 300:
		m.s2xx.Add(1)
	case code < 400:
		m.s3xx.Add(1)
	case code < 500:
		m.s4xx.Add(1)
	default:
		m.s5xx.Add(1)
	}
}

func (m *Metrics) IncInflight()   { m.inflight.Add(1) }
func (m *Metrics) DecInflight()   { m.inflight.Add(-1) }
func (m *Metrics) IncProxyError() { m.proxyErrors.Add(1) }

// IncAuthDenied edge 直接拒掉的请求数（无凭据 / 凭据明确无效）。
func (m *Metrics) IncAuthDenied() { m.authDenied.Add(1) }

// IncVerify 回源校验**拿到权威结论**的次数（缓存未命中且调用成功）。
func (m *Metrics) IncVerify() { m.verifyOK.Add(1) }

// IncVerifyError 回源校验未拿到结论的次数（超时/网络错/非 200/解析失败）。
// 这些请求一律放行交 Python 判定 —— 数字大不一定是故障，也可能是 Python 版本较旧。
func (m *Metrics) IncVerifyError() { m.verifyErr.Add(1) }

// IncCacheHit 命中本地 TTL 缓存（未回源）。
func (m *Metrics) IncCacheHit() { m.cacheHits.Add(1) }

type snapshot struct {
	UptimeSeconds float64          `json:"uptime_seconds"`
	Goroutines    int              `json:"goroutines"`
	BackendURL    string           `json:"backend_url"`
	Inflight      int64            `json:"inflight"`
	Requests      map[string]int64 `json:"requests"`
	Responses     map[string]int64 `json:"responses"`
	ProxyErrors   int64            `json:"proxy_errors"`
	EdgeAuth      authStats        `json:"edge_auth"`
	// Video 视频数据面（S3）的计数，由 video.Stats.Snapshot 提供；
	// 未接线时为 null。字段是 any：metrics 包不该认识视频内部结构。
	Video any `json:"video"`
	// Ingest 入库队列（S4）的计数，由 store.Store.Snapshot 提供；
	// Go 不持有写入权时为 null。
	Ingest any `json:"ingest"`
	// Report 主动汇报 WS 扇出（S4）的计数，由 report.Stats.Snapshot 提供；
	// 未接线时为 null。
	Report any `json:"report"`
}

// authStats 边缘鉴权早拒的计数（未启用时全 0）。
type authStats struct {
	Denied      int64 `json:"denied"`
	VerifyOK    int64 `json:"verify_ok"`
	VerifyError int64 `json:"verify_error"`
	CacheHits   int64 `json:"cache_hits"`
}

// Serve 处理 /metrics（仅内网可达；GET/HEAD）。
func (m *Metrics) Serve(w http.ResponseWriter, r *http.Request) {
	if !httpx.IsPrivateRemote(r.RemoteAddr) {
		// 对外不暴露该路径：与 Python 侧不存在该路由时的 404 形状一致。
		httpx.WriteString(w, r, http.StatusNotFound, "application/json", `{"detail":"Not Found"}`)
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		httpx.WriteString(w, r, http.StatusMethodNotAllowed, "application/json",
			`{"detail":"Method Not Allowed"}`)
		return
	}
	req := make(map[string]int64, len(m.kinds))
	for k, c := range m.kinds {
		req[k.String()] = c.Load()
	}
	snap := snapshot{
		UptimeSeconds: math.Round(time.Since(m.start).Seconds()*10) / 10,
		Goroutines:    runtime.NumGoroutine(),
		BackendURL:    m.backendURL,
		Inflight:      m.inflight.Load(),
		Requests:      req,
		Responses: map[string]int64{
			"1xx": m.s1xx.Load(), "2xx": m.s2xx.Load(), "3xx": m.s3xx.Load(),
			"4xx": m.s4xx.Load(), "5xx": m.s5xx.Load(),
		},
		ProxyErrors: m.proxyErrors.Load(),
		EdgeAuth: authStats{
			Denied:      m.authDenied.Load(),
			VerifyOK:    m.verifyOK.Load(),
			VerifyError: m.verifyErr.Load(),
			CacheHits:   m.cacheHits.Load(),
		},
	}
	if m.videoSource != nil {
		snap.Video = m.videoSource()
	}
	if m.ingestSource != nil {
		snap.Ingest = m.ingestSource()
	}
	if m.reportSource != nil {
		snap.Report = m.reportSource()
	}
	body, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		httpx.WriteString(w, r, http.StatusInternalServerError, "application/json",
			`{"detail":"Internal Server Error"}`)
		return
	}
	httpx.WriteString(w, r, http.StatusOK, "application/json", string(body))
}
