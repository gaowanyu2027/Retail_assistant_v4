// Package store 持久化视频派生数据（S4）。
//
// 唯一写者：`GO_OWNS_INGEST_WRITES=1` 时，只有本包写
// retail_stats / alert_record / track_visit_paths 的**视频派生行**；Python 不再
// 直写这三张表，而是把同一批数据作为上行帧流的 kind=4 事件发来
// （整形在 vision/ingest.py，字段契约在 contracts/ingest_events.json）。
//
// 结构（为什么不是"收到一条写一条"）：
//
//   - 入口 chan + 每表一个 worker：入口满时**丢弃并记账**，绝不反压上行流
//     —— 数据库慢不能把视频帧流一起拖停；
//   - 每表单 goroutine 串行写，批次凑够 BatchSize 或等满 FlushInterval 提交
//     一条多行 INSERT；retail_stats 本身是 ON DUPLICATE KEY UPDATE（幂等），
//     批量语义与 Python 的逐条写入等价；
//   - 写失败重试 Retries 次，仍失败则丢弃该批并计数（Python 原实现同样是
//     "写失败只记日志、不中断处理线程"）；
//   - 事件按 (gen, seq) 去重：上行重连后即使重复投递也不会重复写。
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/go-sql-driver/mysql"
)

const timeLayout = "2006-01-02 15:04:05"

// Config 落库参数。零值字段由 withDefaults 补齐。
type Config struct {
	Host          string
	Port          int
	User          string
	Password      string
	Database      string
	QueueSize     int           // 入口队列容量（事件数）
	BatchSize     int           // 每批最多行数
	FlushInterval time.Duration // 批次最长等待
	WriteTimeout  time.Duration // 单次提交超时
	Retries       int           // 单批最多尝试次数
	RetryBackoff  time.Duration // 重试退避基数（第 n 次等待 n×Backoff）
	CloseTimeout  time.Duration // Close 等待队列收尾的上限
}

func withDefaults(c Config) Config {
	if c.Host == "" {
		c.Host = "mysql"
	}
	if c.Port <= 0 {
		c.Port = 3306
	}
	if c.User == "" {
		c.User = "root"
	}
	if c.Database == "" {
		c.Database = "retail_assistant"
	}
	if c.QueueSize <= 0 {
		c.QueueSize = 4096
	}
	if c.BatchSize <= 0 {
		c.BatchSize = 50
	}
	if c.FlushInterval <= 0 {
		c.FlushInterval = 500 * time.Millisecond
	}
	if c.WriteTimeout <= 0 {
		c.WriteTimeout = 3 * time.Second
	}
	if c.Retries <= 0 {
		c.Retries = 3
	}
	if c.RetryBackoff <= 0 {
		c.RetryBackoff = 100 * time.Millisecond
	}
	if c.CloseTimeout <= 0 {
		c.CloseTimeout = 5 * time.Second
	}
	return c
}

// rawIngest 入口队列元素：meta 已由 video 侧解析出 gen/seq，整段 JSON 留到
// dispatcher 再解析（上行读取 goroutine 只做一次拷贝，不做业务解析）。
type rawIngest struct {
	gen  int64
	seq  int64
	meta []byte
}

// executor 三张表的实际提交动作。抽成接口是为了让批处理/重试/去重逻辑
// 能在没有 MySQL 的环境里被测试（与 route_parity 等"离线优先"的做法一致）。
type executor interface {
	execRetail(ctx context.Context, rows []retailRow) error
	execAlerts(ctx context.Context, rows []alertRow) error
	execPaths(ctx context.Context, rows []pathRow) error
}

// Store 入库队列与三个表 worker。
type Store struct {
	cfg    Config
	writer executor
	db     *sql.DB

	in       chan rawIngest
	retailCh chan []retailRow
	alertCh  chan []alertRow
	pathCh   chan []pathRow

	// dispatcher 独占（单 goroutine），无需加锁
	lastGen int64
	lastSeq int64

	counters
	retailStats tableStats
	alertStats  tableStats
	pathStats   tableStats

	runOnce   sync.Once
	closeOnce sync.Once
	cancel    context.CancelFunc
	started   chan struct{}
	done      chan struct{}
}

type counters struct {
	received     atomic.Uint64
	queueDropped atomic.Uint64 // 入口队列满（事件整条丢弃）
	tableDropped atomic.Uint64 // 某表队列满（行丢弃）
	malformed    atomic.Uint64
	deduped      atomic.Uint64
}

// tableStats 单表的写入结果。
type tableStats struct {
	written     atomic.Uint64
	failed      atomic.Uint64 // 重试后仍失败、被丢弃的行数
	writeErrors atomic.Uint64 // 单次提交失败次数（含重试）
	lastLogNano atomic.Int64
}

// Stats /metrics 的 "ingest" 段（字段名与 vision/ingest.py 的表名对应）。
type Stats struct {
	Received     uint64 `json:"received"`
	QueueDropped uint64 `json:"queue_dropped"`
	TableDropped uint64 `json:"table_dropped"`
	Malformed    uint64 `json:"malformed"`
	Deduped      uint64 `json:"deduped"`
	WrittenRows  uint64 `json:"written_rows"`
	FailedRows   uint64 `json:"failed_rows"`
	WriteErrors  uint64 `json:"write_errors"`
	RetailRows   uint64 `json:"retail_rows"`
	AlertRows    uint64 `json:"alert_rows"`
	PathRows     uint64 `json:"path_rows"`
}

// Open 构造 Store。**不主动连接数据库**：MySQL 暂时不可用时 edge 仍应启动，
// 连接由 database/sql 惰性建立，写失败按批重试。
func Open(cfg Config) (*Store, error) {
	cfg = withDefaults(cfg)
	db, err := sql.Open("mysql", dsn(cfg))
	if err != nil {
		return nil, fmt.Errorf("store: 打开 MySQL: %w", err)
	}
	db.SetMaxOpenConns(3)
	db.SetMaxIdleConns(3)
	db.SetConnMaxLifetime(3 * time.Minute)
	return newStore(cfg, &mysqlWriter{db: db}, db), nil
}

// newStore 测试入口：writer 可以是假实现，db 可以为 nil。
func newStore(cfg Config, writer executor, db *sql.DB) *Store {
	cfg = withDefaults(cfg)
	return &Store{
		cfg:      cfg,
		writer:   writer,
		db:       db,
		in:       make(chan rawIngest, cfg.QueueSize),
		retailCh: make(chan []retailRow, 64),
		alertCh:  make(chan []alertRow, 64),
		pathCh:   make(chan []pathRow, 64),
		lastGen:  -1,
		lastSeq:  -1,
		started:  make(chan struct{}),
		done:     make(chan struct{}),
	}
}

// EnqueueIngest 实现 video.IngestSink。**永不阻塞**：上行读取 goroutine 不能
// 因数据库慢而停摆；队列满时整条事件丢弃并计数。
func (s *Store) EnqueueIngest(gen, seq int64, meta []byte) {
	s.counters.received.Add(1)
	raw := rawIngest{gen: gen, seq: seq, meta: append([]byte(nil), meta...)}
	select {
	case s.in <- raw:
	default:
		s.counters.queueDropped.Add(1)
		slog.Warn("入库队列已满，事件已丢弃", "gen", gen, "seq", seq)
	}
}

// Run 启动三个表 worker 并在当前 goroutine 跑 dispatcher；ctx 取消或 Close 后
// 先把队列里已有的事件分发完，再关闭表队列让 worker 刷出最后一批。
func (s *Store) Run(ctx context.Context) {
	s.runOnce.Do(func() {
		ctx, cancel := context.WithCancel(ctx)
		s.cancel = cancel
		// close(started) 同时是 s.cancel 的发布点：Close 先等这个通道，
		// 因此不会读到 nil cancel，也不必在 Close 里加锁。
		close(s.started)

		var wg sync.WaitGroup
		wg.Add(3)
		go func() {
			defer wg.Done()
			runTable(s.retailCh, s.cfg, s.writer.execRetail, &s.retailStats)
		}()
		go func() {
			defer wg.Done()
			runTable(s.alertCh, s.cfg, s.writer.execAlerts, &s.alertStats)
		}()
		go func() {
			defer wg.Done()
			runTable(s.pathCh, s.cfg, s.writer.execPaths, &s.pathStats)
		}()

		s.dispatch(ctx)
		close(s.retailCh)
		close(s.alertCh)
		close(s.pathCh)
		wg.Wait()
		close(s.done)
	})
}

// Close 停止接收并等待队列收尾（有上限）。可重复调用。
//
// Run 尚未启动时不必等待：没有 worker、没有已接收的事件，直接关数据库即可。
func (s *Store) Close() {
	s.closeOnce.Do(func() {
		select {
		case <-s.started:
			s.cancel()
			select {
			case <-s.done:
			case <-time.After(s.cfg.CloseTimeout):
				slog.Warn("入库队列收尾超时，未写完的数据已放弃", "timeout", s.cfg.CloseTimeout.String())
			}
		case <-time.After(s.cfg.CloseTimeout):
			slog.Warn("入库队列未启动，未写完的数据已放弃", "timeout", s.cfg.CloseTimeout.String())
		}
		if s.db != nil {
			_ = s.db.Close()
		}
	})
}

// MetricsSource /metrics 的 "ingest" 段取值函数（挂给 metrics.Metrics）。
func (s *Store) MetricsSource() func() any {
	return func() any { return s.Snapshot() }
}

// Snapshot 当前计数（全部原子读）。
func (s *Store) Snapshot() Stats {
	return Stats{
		Received:     s.counters.received.Load(),
		QueueDropped: s.counters.queueDropped.Load(),
		TableDropped: s.counters.tableDropped.Load(),
		Malformed:    s.counters.malformed.Load(),
		Deduped:      s.counters.deduped.Load(),
		WrittenRows:  s.retailStats.written.Load() + s.alertStats.written.Load() + s.pathStats.written.Load(),
		FailedRows:   s.retailStats.failed.Load() + s.alertStats.failed.Load() + s.pathStats.failed.Load(),
		WriteErrors:  s.retailStats.writeErrors.Load() + s.alertStats.writeErrors.Load() + s.pathStats.writeErrors.Load(),
		RetailRows:   s.retailStats.written.Load(),
		AlertRows:    s.alertStats.written.Load(),
		PathRows:     s.pathStats.written.Load(),
	}
}

// dispatch 单 goroutine：去重 → 解析 → 按表投递。ctx 取消后把入口队列里
// 已经收到的事件尽量分发完（不丢已接收的数据），然后收尾。
func (s *Store) dispatch(ctx context.Context) {
	for {
		select {
		case raw := <-s.in:
			s.route(raw)
		case <-ctx.Done():
			for {
				select {
				case raw := <-s.in:
					s.route(raw)
				default:
					return
				}
			}
		}
	}
}

func (s *Store) route(raw rawIngest) {
	if raw.gen <= 0 || raw.seq <= 0 {
		s.counters.malformed.Add(1)
		return
	}
	// 去重水位：事件在一条上行流内严格递增；重连后 Python 的游标从当前 seq
	// 起读，不会重放，这里再挡一层重复投递。
	if raw.gen < s.lastGen || (raw.gen == s.lastGen && raw.seq <= s.lastSeq) {
		s.counters.deduped.Add(1)
		return
	}
	s.lastGen, s.lastSeq = raw.gen, raw.seq

	var msg ingestMessage
	if err := json.Unmarshal(raw.meta, &msg); err != nil {
		s.counters.malformed.Add(1)
		slog.Warn("入库事件 meta 解析失败，已丢弃", "gen", raw.gen, "seq", raw.seq, "err", err.Error())
		return
	}

	now := time.Now().Format(timeLayout)
	if r := msg.Retail; r != nil && len(r.Zones) > 0 {
		rows := make([]retailRow, 0, len(r.Zones))
		for _, z := range r.Zones {
			rows = append(rows, retailRow{
				PeriodKey:         r.PeriodKey,
				ZoneID:            z.ZoneID,
				ZoneType:          defaultString(z.ZoneType, "shelf"),
				ZoneLabel:         z.ZoneLabel,
				SavedAt:           defaultString(r.SavedAt, now),
				VisitCount:        z.VisitCount,
				DeepInterestCount: z.DeepInterestCount,
				TotalDwellSeconds: z.TotalDwellSeconds,
				HeatScore:         z.HeatScore,
				Source:            defaultString(z.Source, "video"),
			})
		}
		if !sendBatch(s.retailCh, rows) {
			s.counters.tableDropped.Add(uint64(len(rows)))
		}
	}
	if len(msg.Alerts) > 0 {
		rows := make([]alertRow, 0, len(msg.Alerts))
		for _, a := range msg.Alerts {
			a.CreatedAt = defaultString(a.CreatedAt, now)
			a.Level = defaultString(a.Level, "watch")
			rows = append(rows, a)
		}
		if !sendBatch(s.alertCh, rows) {
			s.counters.tableDropped.Add(uint64(len(rows)))
		}
	}
	if len(msg.Paths) > 0 {
		rows := make([]pathRow, 0, len(msg.Paths))
		for _, p := range msg.Paths {
			encoded, err := json.Marshal(p.Path)
			if err != nil {
				s.counters.malformed.Add(1)
				continue
			}
			rows = append(rows, pathRow{
				SessionID:         truncateRunes(p.SessionID, 128),
				TrackID:           p.TrackID,
				Source:            defaultString(p.Source, "video"),
				PathJSON:          string(encoded),
				ZoneCount:         zoneCountOrLen(p.ZoneCount, len(p.Path)),
				TotalDwellSeconds: p.TotalDwellSeconds,
				RecordedAt:        defaultString(p.RecordedAt, now),
			})
		}
		if !sendBatch(s.pathCh, rows) {
			s.counters.tableDropped.Add(uint64(len(rows)))
		}
	}
}

// sendBatch 非阻塞投递；false = 该表队列已满，调用方按行数记丢弃。
func sendBatch[T any](ch chan []T, rows []T) bool {
	select {
	case ch <- rows:
		return true
	default:
		return false
	}
}

func defaultString(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

func zoneCountOrLen(n, length int) int {
	if n <= 0 {
		return length
	}
	return n
}

// truncateRunes 与 mysql_db.save_track_visit_path 的 `session_id[:128]` 对齐
// （按字符截断，而不是字节，避免把多字节字符切成半个）。
func truncateRunes(s string, limit int) string {
	if limit <= 0 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= limit {
		return s
	}
	return string(runes[:limit])
}

// dsn 用驱动自带的 Config 拼 DSN（口令里的特殊字符会被正确转义）。
func dsn(cfg Config) string {
	c := mysql.NewConfig()
	c.User = cfg.User
	c.Passwd = cfg.Password
	c.Net = "tcp"
	c.Addr = net.JoinHostPort(cfg.Host, strconv.Itoa(cfg.Port))
	c.DBName = cfg.Database
	c.ParseTime = true
	c.Loc = time.Local
	c.Params = map[string]string{"charset": "utf8mb4"}
	c.Timeout = 5 * time.Second
	c.ReadTimeout = 10 * time.Second
	c.WriteTimeout = 10 * time.Second
	return c.FormatDSN()
}

// runTable 单个表的批处理循环：凑批 → 提交 → 失败重试。
func runTable[T any](ch <-chan []T, cfg Config, write func(context.Context, []T) error,
	stats *tableStats) {
	batch := make([]T, 0, cfg.BatchSize)
	timer := time.NewTimer(cfg.FlushInterval)
	defer timer.Stop()

	flush := func() {
		if len(batch) == 0 {
			return
		}
		rows := batch
		batch = make([]T, 0, cfg.BatchSize)
		if writeWithRetry(rows, cfg, write, stats) {
			stats.written.Add(uint64(len(rows)))
		} else {
			stats.failed.Add(uint64(len(rows)))
		}
	}
	resetTimer := func() {
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
		timer.Reset(cfg.FlushInterval)
	}

	for {
		select {
		case rows, ok := <-ch:
			if !ok {
				flush()
				return
			}
			batch = append(batch, rows...)
			if len(batch) >= cfg.BatchSize {
				flush()
				resetTimer()
			}
		case <-timer.C:
			flush()
			timer.Reset(cfg.FlushInterval)
		}
	}
}

func writeWithRetry[T any](rows []T, cfg Config,
	write func(context.Context, []T) error, stats *tableStats) bool {
	var lastErr error
	for attempt := 1; attempt <= cfg.Retries; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), cfg.WriteTimeout)
		err := write(ctx, rows)
		cancel()
		if err == nil {
			return true
		}
		lastErr = err
		stats.writeErrors.Add(1)
		if attempt < cfg.Retries {
			time.Sleep(time.Duration(attempt) * cfg.RetryBackoff)
		}
	}
	logFailure(stats, len(rows), lastErr)
	return false
}

// logFailure 数据库持续不可用时每 10 秒最多记一条，避免每批都刷屏。
func logFailure(stats *tableStats, rows int, err error) {
	now := time.Now().UnixNano()
	last := stats.lastLogNano.Load()
	if now-last < int64(10*time.Second) {
		return
	}
	if stats.lastLogNano.CompareAndSwap(last, now) {
		slog.Error("入库批次写入失败，已丢弃", "rows", rows, "err", err.Error())
	}
}
