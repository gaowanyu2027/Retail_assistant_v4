// Package report 接管主动汇报的 WebSocket 通道（/api/ws/reports 与兼容别名），
// 把订阅连接与扇出从 Python 的模块级单例（`_report_clients` / `_report_loop`）
// 移到 edge。
//
// 边界（与三条铁律一致）：
//   - 汇报内容由 Python 生成（报表 Agent / 定时任务），edge 不解析业务字段；
//   - Python 通过内网端点 `POST /internal/report/broadcast` 把消息交给 edge，
//     由 edge 扇出给所有订阅者；
//   - 连接、心跳、慢客户端处理全部在 edge，Python 不再持有订阅连接。
//
// 与视频数据面不同，汇报 WS 迁移前就是**匿名可订阅**的（api/routes/report.py
// 没有鉴权依赖），所以接管不要求 EDGE_AUTH。唯一前提是 INTERNAL_API_TOKEN
// 非空：广播入口必须能校验调用方，宁可退回"全部反代"也不裸奔。
package report

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync/atomic"

	"retail-assistant/edge/internal/auth"
	"retail-assistant/edge/internal/httpx"
	"retail-assistant/edge/internal/routing"
	"retail-assistant/edge/internal/wsutil"
)

const (
	// 两条被接管的 WS 端点（与 api/routes/report.py 一一对应）。
	pathReports       = "/api/ws/reports"
	pathReportsLegacy = "/api/report/ws/reports"

	// internalPath Python → edge 的广播入口。与其它 /internal/** 一样只在内网可达。
	internalPath = "/internal/report/broadcast"

	// maxBroadcastBytes 单条汇报上限。汇报是摘要 + 少量区域数据（实测几 KB），
	// 1MiB 足够；同时避免内网端点被误用成大文件上传。
	maxBroadcastBytes = 1 << 20
)

// Options 汇报扇出的构造参数。
type Options struct {
	// Enabled 对应 EDGE_REPORT=1（功能总开关）。
	Enabled bool
	// InternalToken 与 Python 共享的密钥（INTERNAL_API_TOKEN）。
	// 为空且 Enabled=1 时整个功能禁用：广播入口没有密钥可校验。
	InternalToken string
}

// Handler 接管汇报 WS 与 Python 的广播入口。
type Handler struct {
	enabled bool
	token   string
	stats   *Stats
	hub     *Hub
	closed  atomic.Bool
}

// New 构造汇报扇出。Enabled() 为假时 Handle 一律不接管（WS 照旧反代给 Python）。
func New(o Options) *Handler {
	h := &Handler{
		enabled: o.Enabled && o.InternalToken != "",
		token:   o.InternalToken,
	}
	h.stats = newStats(h.enabled)
	h.hub = newHub(h.stats)
	return h
}

// Enabled 是否具备接管条件（EDGE_REPORT=1 且共享密钥非空）。
func (h *Handler) Enabled() bool { return h.enabled }

// MetricsSource /metrics 的 "report" 段取值函数（挂给 metrics.Metrics）。
func (h *Handler) MetricsSource() func() any { return h.stats.Snapshot }

// Handle 尝试接管一条汇报 WS 请求。返回 (归类, 是否已处理)：
//
//	(routing.Report, true)  已接管（升级成功，或升级失败但请求已由本函数结束）
//	(routing.Proxy, false)  不归我管 / 未启用 —— 调用方照旧走兜底反代
//
// 只有**真正的 WS 升级请求**才会被接管：同一路径上的普通 HTTP 请求语义不变，
// 仍交给 Python（route_parity 的差异为 0 由构造保证）。
func (h *Handler) Handle(w *httpx.RespWriter, r *http.Request) (routing.Kind, bool) {
	if !isReportPath(r.URL.Path) || !wsutil.IsUpgrade(r) {
		return routing.Proxy, false
	}
	if !h.enabled || h.closed.Load() {
		return routing.Proxy, false
	}

	conn, err := wsutil.Upgrade(w, r)
	if err != nil {
		// IsUpgrade 已通过，走到这里只可能是 Hijack/写响应失败：连接不可用，
		// 不能再去反代（响应头可能已经写出）。
		slog.Warn("汇报 WS 升级失败", "path", r.URL.Path, "err", err.Error(),
			"ip", httpx.ClientIP(r.RemoteAddr))
		return routing.Report, true
	}
	w.MarkUpgraded(http.StatusSwitchingProtocols)
	h.stats.incConnection()

	ctx, cancel := context.WithCancel(context.Background())
	c := newClient(conn, h.stats)
	c.cancel = cancel
	h.hub.Add(c)
	defer func() {
		cancel()
		_ = conn.Close()
		c.loops.Wait()
		h.hub.Remove(c)
	}()

	c.loops.Add(2)
	go func() { defer c.loops.Done(); defer cancel(); c.writeLoop(ctx) }()
	go func() { defer c.loops.Done(); defer cancel(); c.readLoop(ctx) }()
	go conn.Keepalive(ctx, wsutil.PingInterval)
	<-ctx.Done()
	return routing.Report, true
}

// HandleInternal 处理 Python 的广播入口。
//
// 返回 false = 不是这个路径（调用方按普通 /internal/** 返回 404）。
// 返回 true  = 已处理（成功 200；任何门禁失败一律 404，与 Python 内部端点
// 同形：不泄露端点是否存在）。
func (h *Handler) HandleInternal(w *httpx.RespWriter, r *http.Request) bool {
	if r.URL.Path != internalPath {
		return false
	}
	if !h.enabled || h.closed.Load() {
		return false // 未启用：交给通用 /internal/** 404
	}
	if r.Method != http.MethodPost ||
		!httpx.IsPrivateRemote(r.RemoteAddr) ||
		!tokenEqual(r.Header.Get("X-Internal-Token"), h.token) {
		h.stats.incDenied()
		auth.ServeInternalNotFound(w, r)
		return true
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, maxBroadcastBytes+1))
	if err != nil || len(body) > maxBroadcastBytes || !isJSONObject(body) {
		h.stats.incInvalid()
		httpx.WriteString(w, r, http.StatusBadRequest, "application/json",
			`{"detail":"invalid report payload"}`)
		return true
	}

	n := h.hub.broadcast(body)
	h.stats.incBroadcast()
	slog.Info("汇报广播", "clients", n, "bytes", len(body))
	httpx.WriteString(w, r, http.StatusOK, "application/json",
		fmt.Sprintf(`{"status":"ok","delivered":%d}`, n))
	return true
}

// Close 关掉全部在管连接（幂等）。**必须在 http.Server.Shutdown 之前调用**：
// WS 握手后连接已被 Hijack，不在 Server 的管理范围内（同 video.Handler.Close）。
func (h *Handler) Close() {
	if !h.enabled || h.closed.Swap(true) {
		return
	}
	h.hub.each(func(c *client) { c.shutdown() })
}

// isReportPath 是否为被接管的汇报 WS 路径。
func isReportPath(path string) bool {
	return path == pathReports || path == pathReportsLegacy
}

// tokenEqual 常数时间比较共享密钥（长度不同直接判否，不泄露长度之外的任何信息）。
func tokenEqual(got, want string) bool {
	if len(got) != len(want) || want == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

// isJSONObject 只接受 JSON 对象：汇报的契约是 {type,summary,data,ts}。
// 不解析字段、不回写 —— 转发给浏览器的就是 Python 给的原始字节。
func isJSONObject(body []byte) bool {
	var obj map[string]json.RawMessage
	// obj != nil 同时排除字面量 null：encoding/json 对 null 解到 map 会成功
	// 但留下 nil map，而汇报契约要求的是一个对象。
	return json.Unmarshal(body, &obj) == nil && obj != nil
}
