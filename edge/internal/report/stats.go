package report

import "sync/atomic"

// Stats 汇报扇出的运行计数。全部可在任意 goroutine 并发调用，
// Snapshot() 输出 JSON 片段，由 metrics.Metrics 挂到 /metrics 的 "report" 段。
//
// 这些数字是 S4 的验收判据：
//   - 接管生效：clients 随订阅连接增减；
//   - Python → edge 通路：broadcasts 随定时汇报增长；
//   - 慢客户端：dropped 增长而 broadcasts/delivered 不被拖住；
//   - 门禁：denied 对应密钥/对端不合法的广播请求。
type Stats struct {
	enabled bool

	clients     atomic.Int64
	connections atomic.Int64
	broadcasts  atomic.Int64
	delivered   atomic.Int64
	dropped     atomic.Int64
	denied      atomic.Int64
	invalid     atomic.Int64
}

// reportSnapshot /metrics 里的 "report" 段。
type reportSnapshot struct {
	Enabled     bool  `json:"enabled"`
	Clients     int64 `json:"clients"`
	Connections int64 `json:"connections"`
	Broadcasts  int64 `json:"broadcasts"`
	Delivered   int64 `json:"delivered"`
	Dropped     int64 `json:"dropped"`
	Denied      int64 `json:"denied"`
	Invalid     int64 `json:"invalid"`
}

func newStats(enabled bool) *Stats { return &Stats{enabled: enabled} }

func (s *Stats) incConnection()   { s.connections.Add(1) }
func (s *Stats) incBroadcast()    { s.broadcasts.Add(1) }
func (s *Stats) incDelivered()    { s.delivered.Add(1) }
func (s *Stats) incDropped()      { s.dropped.Add(1) }
func (s *Stats) incDenied()       { s.denied.Add(1) }
func (s *Stats) incInvalid()      { s.invalid.Add(1) }
func (s *Stats) setClients(n int) { s.clients.Store(int64(n)) }

// Snapshot 供 /metrics 挂载（返回类型 any：metrics 包不该认识汇报内部结构）。
func (s *Stats) Snapshot() any {
	return reportSnapshot{
		Enabled:     s.enabled,
		Clients:     s.clients.Load(),
		Connections: s.connections.Load(),
		Broadcasts:  s.broadcasts.Load(),
		Delivered:   s.delivered.Load(),
		Dropped:     s.dropped.Load(),
		Denied:      s.denied.Load(),
		Invalid:     s.invalid.Load(),
	}
}
