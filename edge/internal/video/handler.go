package video

import (
	"context"
	"log/slog"
	"net/http"
	"sync/atomic"
	"time"

	"retail-assistant/edge/internal/auth"
	"retail-assistant/edge/internal/httpx"
	"retail-assistant/edge/internal/routing"
	"retail-assistant/edge/internal/wsutil"
)

// 两条被接管的端点（与 api/routes/stream.py 的路由一一对应）。
const (
	endpointStream = "stream"
	endpointClient = "client"

	pathStream = "/api/ws/stream"
	pathClient = "/api/ws/client"
)

// detachTimeout 断开时向 Python 注销观看者的超时。**不挂在连接 ctx 上**
// （连接已经结束了，用 WithoutCancel 另起一个）——注销成功与否影响
// "最后一个观看者离开即停会话"的判定，值得单独给一次机会。
const detachTimeout = 5 * time.Second

// Options 视频数据面的构造参数。
type Options struct {
	// Video 对应 EDGE_VIDEO=1（功能总开关）。
	Video bool
	// AuthEnabled 对应 EDGE_AUTH=1。**接管 WS 的前提**：没有权威鉴权判定，
	// edge 就无法区分"未登录（应答 1008）"和"登录了（该接管）"。
	AuthEnabled bool
	// BackendURL / InternalToken 内网调用 Python（与早拒共用同一份配置）。
	BackendURL    string
	InternalToken string
	// Verifier 与早拒共用同一个判定器（同一份 TTL 缓存，不重复回源）。
	Verifier *auth.Verifier
}

// Handler 接管 `/api/ws/stream` 与 `/api/ws/client` 两条 WS 端点。
//
// 边界（铁律②③）：它**只**做连接、鉴权判定、扇出与协议；像素、动作语义、
// 观看者归属、会话生命周期全部在 Python（经 /internal/vision/* 调用）。
type Handler struct {
	o        Options
	enabled  bool
	stats    *Stats
	hub      *Hub
	api      *API
	upstream *Upstream

	baseCtx context.Context
	cancel  context.CancelFunc
	closed  atomic.Bool
}

// New 构造视频数据面。Enabled() 为假时 Handle 一律不接管（WS 照旧反代给 Python）。
func New(o Options) *Handler {
	h := &Handler{o: o, stats: newStats()}
	h.enabled = o.Video && o.AuthEnabled && o.InternalToken != "" && o.Verifier != nil
	h.api = NewAPI(o.BackendURL, o.InternalToken)
	h.hub = newHub(h.stats)
	h.stats.attachHub(h.hub)
	h.upstream = newUpstream(h.api, h.hub, h.stats)
	return h
}

// Enabled 是否具备接管条件（缺一即禁用，见部署文档的启用前提）。
func (h *Handler) Enabled() bool { return h.enabled }

// MetricsSource /metrics 的 "video" 段取值函数（挂给 metrics.Metrics）。
func (h *Handler) MetricsSource() func() any { return h.stats.Snapshot }

// Start 启动上行订阅（未启用时空操作）。Close 之前一直重连，永不放弃。
func (h *Handler) Start() {
	if !h.enabled {
		return
	}
	h.baseCtx, h.cancel = context.WithCancel(context.Background())
	h.upstream.Start(h.baseCtx)
}

// Close 停掉视频数据面：先停上行订阅，再关掉全部在管连接（幂等）。
//
// **必须在 http.Server.Shutdown 之前调用**：被 Hijack 的连接不在
// Server 的管理范围内，Shutdown 不会等它们，更不会关它们（见 cmd/edge/main.go）。
func (h *Handler) Close() {
	if !h.enabled || h.closed.Swap(true) {
		return
	}
	h.upstream.Close()
	h.hub.each(func(c *client) { c.shutdown() })
}

// Handle 尝试接管一条请求。返回 (归类, 是否已处理)：
//
//	(routing.Video, true)   已接管（升级成功，或未鉴权回了 1008）
//	(routing.Proxy, false)  不归我管 / 拿不准 —— 调用方照旧走兜底反代
//
// 只有**真正的 WS 升级请求**才会被接管：同一个路径上的普通 HTTP 请求
// （探针、误访问）语义不变，route_parity 的差异为 0 由构造保证。
func (h *Handler) Handle(w *httpx.RespWriter, r *http.Request) (routing.Kind, bool) {
	name, ok := videoEndpoint(r.URL.Path)
	if !ok || !wsutil.IsUpgrade(r) {
		return routing.Proxy, false
	}
	if !h.enabled || h.closed.Load() {
		// 未启用 / 正在关闭：交回兜底反代（Python 仍是权威的处理者）。
		return routing.Proxy, false
	}

	user, decision := h.authenticate(r)
	switch decision {
	case decideProxy:
		h.stats.proxied(name)
		return routing.Proxy, false
	case decideDeny:
		h.stats.upgraded(name)
		h.stats.rejected(name)
		slog.Info("WS 未鉴权，按契约 accept 后 close(1008)",
			"endpoint", name, "ip", httpx.ClientIP(r.RemoteAddr))
		h.rejectUnauthorized(w, r)
		return routing.Video, true
	}

	h.stats.upgraded(name)
	h.serve(w, r, name, user)
	return routing.Video, true
}

// ==================== 鉴权决策 ====================

type authDecision int

const (
	decideServe authDecision = iota // 结论权威且有效：接管
	decideProxy                     // 拿不准：交 Python 判（宁可多做一次鉴权，不可误杀）
	decideDeny                      // 结论权威且无效：accept + close(1008)
)

// authenticate 复刻 Python 的 `user_from_scope` 顺序（**顺序不能换**，见各处注释）。
func (h *Handler) authenticate(r *http.Request) (*auth.User, authDecision) {
	// ① 会话凭据（Bearer 优先、Cookie 次之）：逐个试 —— 垃圾 Cookie 不该把有效
	//    Bearer 挡掉（台账 B13）。
	for _, c := range auth.Credentials(r.Header) {
		v := h.o.Verifier.Verify(c.Token)
		if !v.Known {
			return nil, decideProxy
		}
		if v.Valid {
			if v.User == nil {
				// Python 自相矛盾（有效却没给用户）：不猜，交它自己判。
				slog.Warn("鉴权结论有效但缺用户信息，改走反代")
				return nil, decideProxy
			}
			return v.User, decideServe
		}
		// 这枚无效，继续试下一枚
	}

	// ② 一次性票据（小程序用 ?ticket= 连视频）。**只在没有有效会话时消费**：
	//    有有效 Cookie 时 Python 根本不会碰票据，我们也不能把它烧掉 ——
	//    用后即焚的东西，猜错一次就等于废掉一张票。
	q := r.URL.Query()
	if ticket := q.Get("ticket"); ticket != "" {
		v := h.o.Verifier.ConsumeTicket(ticket)
		if !v.Known {
			return nil, decideProxy
		}
		if v.Valid && v.User != nil {
			return v.User, decideServe
		}
	}

	// ③ `?token=`：`AUTH_ALLOW_QUERY_TOKEN` 的政策只在 Python（且要过期/吊销判断），
	//    带它就一律反代 —— 与早拒的"URL 凭据一律放行"同一条边界。
	if q.Get("token") != "" {
		return nil, decideProxy
	}

	// ④ 没有任何可用凭据：与 Python 一致的 accept + close(1008)。
	return nil, decideDeny
}

// rejectUnauthorized 逐字复刻 Python 的未鉴权 WS 语义：**先完成握手再 close(1008)**，
// 不带 reason（浏览器端看到的是"连上了又被拒"，与直连 Python 时相同）。
func (h *Handler) rejectUnauthorized(w *httpx.RespWriter, r *http.Request) {
	conn, err := wsutil.Upgrade(w, r)
	if err != nil {
		slog.Warn("未鉴权 WS 的握手失败", "err", err.Error())
		return
	}
	w.MarkUpgraded(http.StatusSwitchingProtocols)
	_ = conn.CloseWith(wsutil.StatusPolicyViolation, "")
}

// ==================== 接管 ====================

// serve 一次连接会话：升级 → 登记 → 建立读写循环 → 阻塞到连接结束 → 收尾。
func (h *Handler) serve(w *httpx.RespWriter, r *http.Request, name string, user *auth.User) {
	conn, err := wsutil.Upgrade(w, r)
	if err != nil {
		slog.Warn("WS 升级失败", "endpoint", name, "err", err.Error(),
			"ip", httpx.ClientIP(r.RemoteAddr))
		return
	}
	w.MarkUpgraded(http.StatusSwitchingProtocols)

	ctx, cancel := context.WithCancel(h.baseCtx)
	c := newClient(name, conn, h.api, h.stats)
	c.cancel = cancel
	c.setViewer("", user.Username)

	// 连接即登记：Python 只有知道"有观看者"才会让会话继续跑
	// （"最后一个观看者离开即停会话"）。
	if sync, err := h.api.Attach(ctx, "", user.Username, nil); err == nil {
		c.setViewer(sync.Viewer, user.Username)
	} else {
		slog.Warn("观看者登记失败（连接照常建立，首帧时会再试）",
			"endpoint", name, "err", err.Error())
	}

	h.hub.Add(c)
	h.upstream.Kick()
	defer h.teardown(ctx, c)

	if name == endpointClient {
		h.serveClient(ctx, cancel, c)
	} else {
		h.serveStream(ctx, cancel, c)
	}
}

// serveStream 观看端：读侧=客户端动作与 ping，写侧=帧与控制文本（唯一的写者）。
func (h *Handler) serveStream(ctx context.Context, cancel context.CancelFunc, c *client) {
	c.loops.Add(2)
	go func() { defer c.loops.Done(); c.writeLoop(ctx); cancel() }()
	go func() { defer c.loops.Done(); c.readLoop(ctx); cancel() }()
	go c.conn.Keepalive(ctx, wsutil.PingInterval)
	<-ctx.Done()
}

// serveClient 浏览器本机摄像头端：先回 ready，随后是"最新帧合并"的上行。
// 这一路没有下行帧，写只有 ready 与 pong（wsutil 内部串行化写）。
func (h *Handler) serveClient(ctx context.Context, cancel context.CancelFunc, c *client) {
	if err := c.conn.WriteText(readyMessage); err != nil {
		cancel()
		return
	}
	c.loops.Add(1)
	go func() { defer c.loops.Done(); c.readUploads(ctx); cancel() }()
	go c.pumpUploads(ctx)
	go c.conn.Keepalive(ctx, wsutil.PingInterval)
	<-ctx.Done()
}

// teardown 连接收尾。顺序有讲究：
//
//	取消 ctx（各循环看到即停） → 关闭连接（唤醒仍阻塞在 Read/Write 的一侧）
//	→ 等循环退出 → **先摘除再注销**（免得把 stop 事件投给已经离开的连接）
//	→ 通知 Python 注销观看者。
func (h *Handler) teardown(ctx context.Context, c *client) {
	c.cancel()
	_ = c.conn.Close()
	c.loops.Wait()
	h.hub.Remove(c)

	// 用 WithoutCancel：连接已经结束，这次注销不该被"请求已结束"取消掉
	// （它决定会话是否停 —— 是"最后一个观看者离开"的判定输入）。
	detachCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), detachTimeout)
	defer cancel()
	viewer := c.viewer()
	if viewer == "" {
		return // 从没登记成功过：Python 那边没有它，无需注销
	}
	if err := h.api.Detach(detachCtx, viewer); err != nil {
		slog.Warn("观看者注销失败", "endpoint", c.endpoint, "viewer", viewer, "err", err.Error())
	}
}

// videoEndpoint 路径 → 端点名（不是被接管的路径则 ok=false）。
func videoEndpoint(path string) (string, bool) {
	switch path {
	case pathStream:
		return endpointStream, true
	case pathClient:
		return endpointClient, true
	default:
		return "", false
	}
}
