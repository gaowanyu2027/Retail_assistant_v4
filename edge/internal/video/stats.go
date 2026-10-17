package video

import (
	"sync"
	"sync/atomic"
	"time"
)

// Stats 视频数据面的运行计数。全部可在任意 goroutine 并发调用，
// Snapshot() 输出 JSON 片段，由 metrics.Metrics 挂到 /metrics 的 "video" 段。
//
// 这些数字是 S3 的验收判据（"先测量、再断言"）：
//   - 扇出公平性：慢客户端注入时，快客户端连接上的 frames_dropped 必须保持 0；
//   - 无泄漏：连接/断开 200 次后 viewers 回零、goroutine 数回基线；
//   - 上行健康：last_event_age_s（心跳每秒一次，明显大于 4 秒说明上行已经不正常）。
type Stats struct {
	upstreamLive atomic.Bool
	frames       atomic.Int64
	controls     atomic.Int64
	heartbeats   atomic.Int64
	decodeErrors atomic.Int64
	reconnects   atomic.Int64
	lastEventAt  atomic.Int64 // 最近一次上行事件（unix nano；0 = 还没收到过）
	viewers      atomic.Int64

	mu         sync.Mutex
	hub        *Hub
	byEndpoint map[string]*endpointStats
}

// endpointStats 一条 WS 端点上的处理方式计数。
type endpointStats struct {
	upgraded atomic.Int64 // edge 接管（含鉴权通过后接管 / 无凭据时 accept+close(1008)）
	proxied  atomic.Int64 // 交回兜底反代（凭据拿不准、`?token=`、功能未启用）
	rejected atomic.Int64 // 接管的连接里因未鉴权被 close(1008) 的
}

// viewerStats 一条连接的计数（/metrics 的可读输出）。
type viewerStats struct {
	Endpoint      string `json:"endpoint"`
	Viewer        string `json:"viewer"`
	Username      string `json:"username"`
	Gen           int    `json:"gen"`
	FramesSent    int64  `json:"frames_sent"`
	FramesDropped int64  `json:"frames_dropped"`
	TextsDropped  int64  `json:"texts_dropped"`
	LastFrameID   int64  `json:"last_frame_id"`
}

// videoSnapshot /metrics 里的 "video" 段。
type videoSnapshot struct {
	Upstream        bool                     `json:"upstream_connected"`
	LastEventAgeSec float64                  `json:"last_event_age_s"`
	Viewers         int64                    `json:"viewers"`
	Frames          int64                    `json:"frames"`
	Controls        int64                    `json:"controls"`
	Heartbeats      int64                    `json:"heartbeats"`
	DecodeErrors    int64                    `json:"decode_errors"`
	Reconnects      int64                    `json:"reconnects"`
	Endpoints       map[string]endpointCount `json:"endpoints"`
	Connections     []viewerStats            `json:"connections"`
}

type endpointCount struct {
	Upgraded int64 `json:"upgraded"`
	Proxied  int64 `json:"proxied"`
	Rejected int64 `json:"rejected"`
}

func newStats() *Stats {
	return &Stats{
		byEndpoint: map[string]*endpointStats{
			endpointStream: {},
			endpointClient: {},
		},
	}
}

// attachHub 接上连接表（Snapshot 要导出每连接的丢帧数）。构造顺序：先 Stats 再 Hub。
func (s *Stats) attachHub(h *Hub) { s.hub = h }

func (s *Stats) upstreamUp()          { s.upstreamLive.Store(true) }
func (s *Stats) upstreamDown()        { s.upstreamLive.Store(false) }
func (s *Stats) frame()               { s.frames.Add(1) }
func (s *Stats) control()             { s.controls.Add(1) }
func (s *Stats) heartbeat()           { s.heartbeats.Add(1) }
func (s *Stats) decodeError()         { s.decodeErrors.Add(1) }
func (s *Stats) reconnect()           { s.reconnects.Add(1) }
func (s *Stats) heartbeatAt(ts int64) { s.lastEventAt.Store(ts) }

// setViewers 当前在管连接数（hub 每次增删都调）。
func (s *Stats) setViewers(n int) { s.viewers.Store(int64(n)) }

// endpoint 取某条端点的计数桶（只会是 stream / client 两个已知键）。
func (s *Stats) endpoint(name string) *endpointStats {
	s.mu.Lock()
	e := s.byEndpoint[name]
	if e == nil {
		e = &endpointStats{}
		s.byEndpoint[name] = e
	}
	s.mu.Unlock()
	return e
}

func (s *Stats) upgraded(name string) { s.endpoint(name).upgraded.Add(1) }
func (s *Stats) proxied(name string)  { s.endpoint(name).proxied.Add(1) }
func (s *Stats) rejected(name string) { s.endpoint(name).rejected.Add(1) }

// Snapshot 供 /metrics 挂载（返回类型 any：metrics 包不该认识视频内部结构）。
func (s *Stats) Snapshot() any {
	snap := videoSnapshot{
		Upstream:     s.upstreamLive.Load(),
		Viewers:      s.viewers.Load(),
		Frames:       s.frames.Load(),
		Controls:     s.controls.Load(),
		Heartbeats:   s.heartbeats.Load(),
		DecodeErrors: s.decodeErrors.Load(),
		Reconnects:   s.reconnects.Load(),
		Endpoints:    make(map[string]endpointCount, len(s.byEndpoint)),
		Connections:  []viewerStats{},
	}
	if ts := s.lastEventAt.Load(); ts != 0 {
		snap.LastEventAgeSec = time.Since(time.Unix(0, ts)).Round(time.Millisecond).Seconds()
	}
	s.mu.Lock()
	for name, c := range s.byEndpoint {
		snap.Endpoints[name] = endpointCount{
			Upgraded: c.upgraded.Load(), Proxied: c.proxied.Load(), Rejected: c.rejected.Load(),
		}
	}
	s.mu.Unlock()
	if s.hub != nil {
		for _, c := range s.hub.snapshot() {
			snap.Connections = append(snap.Connections, c.snapshot())
		}
	}
	return snap
}

// snapshot 导出一条连接的计数。
func (c *client) snapshot() viewerStats {
	c.mu.Lock()
	vs := viewerStats{
		Endpoint: c.endpoint, Viewer: c.viewerID, Username: c.username,
		Gen: c.runGen, LastFrameID: c.lastFrameID.Load(),
		FramesSent: c.framesSent.Load(), FramesDropped: c.framesDropped.Load(),
		TextsDropped: c.textsDropped,
	}
	c.mu.Unlock()
	return vs
}
