package store

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeWriter 记录每次提交的行；可以按表注入失败次数（<0 = 总是失败）。
type fakeWriter struct {
	mu sync.Mutex

	retailRows []retailRow
	alertRows  []alertRow
	pathRows   []pathRow

	retailCalls int
	alertCalls  int
	pathCalls   int

	retailFail int
	alertFail  int
	pathFail   int
}

var errFakeWrite = errors.New("fake write failure")

func (f *fakeWriter) execRetail(_ context.Context, rows []retailRow) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.retailCalls++
	if f.retailFail != 0 {
		if f.retailFail > 0 {
			f.retailFail--
		}
		return errFakeWrite
	}
	f.retailRows = append(f.retailRows, rows...)
	return nil
}

func (f *fakeWriter) execAlerts(_ context.Context, rows []alertRow) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.alertCalls++
	if f.alertFail != 0 {
		if f.alertFail > 0 {
			f.alertFail--
		}
		return errFakeWrite
	}
	f.alertRows = append(f.alertRows, rows...)
	return nil
}

func (f *fakeWriter) execPaths(_ context.Context, rows []pathRow) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.pathCalls++
	if f.pathFail != 0 {
		if f.pathFail > 0 {
			f.pathFail--
		}
		return errFakeWrite
	}
	f.pathRows = append(f.pathRows, rows...)
	return nil
}

func (f *fakeWriter) snapshot() (retail []retailRow, alerts []alertRow, paths []pathRow, calls [3]int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	retail = append([]retailRow(nil), f.retailRows...)
	alerts = append([]alertRow(nil), f.alertRows...)
	paths = append([]pathRow(nil), f.pathRows...)
	calls = [3]int{f.retailCalls, f.alertCalls, f.pathCalls}
	return
}

func newTestStore(w executor, tweak func(*Config)) *Store {
	cfg := Config{
		BatchSize:     100,
		FlushInterval: time.Hour,
		Retries:       2,
		RetryBackoff:  time.Millisecond,
		CloseTimeout:  2 * time.Second,
	}
	if tweak != nil {
		tweak(&cfg)
	}
	return newStore(cfg, w, nil)
}

// startStore 在后台跑 Run；Cleanup 保证取消、关闭、等待退出。
func startStore(t *testing.T, s *Store) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		s.Run(ctx)
		close(done)
	}()
	t.Cleanup(func() {
		cancel()
		s.Close()
		<-done
	})
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("等待超时：%s", what)
}

func retailMeta(t *testing.T, gen, seq int64, zones ...retailZone) []byte {
	t.Helper()
	msg := ingestMessage{
		Gen: gen, Seq: seq,
		Retail: &retailBatch{
			PeriodKey: "202610171605",
			SavedAt:   "2026-10-17 16:05:00",
			Zones:     zones,
		},
	}
	b, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("构造 retail 事件: %v", err)
	}
	return b
}

func TestFlushesWhenBatchSizeReached(t *testing.T) {
	w := &fakeWriter{}
	s := newTestStore(w, func(c *Config) { c.BatchSize = 2 })
	startStore(t, s)

	zone := retailZone{ZoneID: "shelf_1", VisitCount: 1}
	s.EnqueueIngest(1, 1, retailMeta(t, 1, 1, zone))
	s.EnqueueIngest(1, 2, retailMeta(t, 1, 2, zone))

	waitFor(t, "两条事件凑成一批", func() bool {
		rows, _, _, _ := w.snapshot()
		return len(rows) == 2
	})
	rows, _, _, calls := w.snapshot()
	if calls[0] != 1 {
		t.Fatalf("两次单 zone 事件应合并为一次提交，实际 %d 次", calls[0])
	}
	if rows[0].ZoneID != "shelf_1" || rows[1].VisitCount != 1 {
		t.Fatalf("行内容不符: %+v", rows)
	}
}

func TestFlushesOnInterval(t *testing.T) {
	w := &fakeWriter{}
	s := newTestStore(w, func(c *Config) {
		c.BatchSize = 100
		c.FlushInterval = 10 * time.Millisecond
	})
	startStore(t, s)

	s.EnqueueIngest(1, 1, retailMeta(t, 1, 1, retailZone{ZoneID: "shelf_1"}))

	waitFor(t, "等待 FlushInterval 触发", func() bool {
		rows, _, _, _ := w.snapshot()
		return len(rows) == 1
	})
}

func TestDedupsByGenerationAndSequence(t *testing.T) {
	w := &fakeWriter{}
	s := newTestStore(w, nil)
	startStore(t, s)

	s.EnqueueIngest(2, 5, retailMeta(t, 2, 5, retailZone{ZoneID: "shelf_1"}))
	s.EnqueueIngest(2, 5, retailMeta(t, 2, 5, retailZone{ZoneID: "shelf_1"})) // 重复投递
	s.EnqueueIngest(2, 4, retailMeta(t, 2, 4, retailZone{ZoneID: "shelf_2"})) // 更早的序号
	s.EnqueueIngest(1, 9, retailMeta(t, 1, 9, retailZone{ZoneID: "shelf_3"})) // 上一代

	waitFor(t, "去重计数到 3", func() bool { return s.Snapshot().Deduped == 3 })
	s.Close()

	rows, _, _, _ := w.snapshot()
	if len(rows) != 1 || rows[0].ZoneID != "shelf_1" {
		t.Fatalf("只应写入第一条事件，实际 %+v", rows)
	}
}

func TestMalformedEventsAreCounted(t *testing.T) {
	w := &fakeWriter{}
	s := newTestStore(w, nil)
	startStore(t, s)

	s.EnqueueIngest(0, 1, retailMeta(t, 0, 1, retailZone{ZoneID: "bad_gen"}))
	s.EnqueueIngest(3, 0, retailMeta(t, 3, 0, retailZone{ZoneID: "bad_seq"}))
	s.EnqueueIngest(3, 1, []byte(`{"gen":3,"seq":1,"alerts":`))

	waitFor(t, "三条事件全部判为格式错误", func() bool { return s.Snapshot().Malformed == 3 })
	s.Close()
	if _, _, _, calls := w.snapshot(); calls != [3]int{} {
		t.Fatalf("格式错误的事件不应触发写入，实际调用 %v", calls)
	}
}

func TestQueueFullDropsWithoutBlocking(t *testing.T) {
	w := &fakeWriter{}
	// 不启动 Run：入口队列容量 1，第二条必然放不进去。
	s := newStore(Config{QueueSize: 1, CloseTimeout: 10 * time.Millisecond}, w, nil)
	s.EnqueueIngest(1, 1, retailMeta(t, 1, 1, retailZone{ZoneID: "a"}))
	s.EnqueueIngest(1, 2, retailMeta(t, 1, 2, retailZone{ZoneID: "b"}))

	stats := s.Snapshot()
	if stats.Received != 2 || stats.QueueDropped != 1 {
		t.Fatalf("received=%d queue_dropped=%d，期望 2/1", stats.Received, stats.QueueDropped)
	}
	s.Close() // Run 未启动：有上限地返回，不阻塞调用方
}

func TestWriteRetriesThenSucceeds(t *testing.T) {
	w := &fakeWriter{retailFail: 2}
	s := newTestStore(w, func(c *Config) {
		c.BatchSize = 10
		c.FlushInterval = 10 * time.Millisecond
		c.Retries = 3
	})
	startStore(t, s)

	s.EnqueueIngest(1, 1, retailMeta(t, 1, 1,
		retailZone{ZoneID: "shelf_1"}, retailZone{ZoneID: "shelf_2"}))

	waitFor(t, "重试后写入成功", func() bool {
		rows, _, _, _ := w.snapshot()
		return len(rows) == 2
	})
	stats := s.Snapshot()
	if stats.WriteErrors != 2 {
		t.Fatalf("write_errors=%d，期望 2 次失败后成功", stats.WriteErrors)
	}
	if stats.FailedRows != 0 {
		t.Fatalf("failed_rows=%d，期望 0", stats.FailedRows)
	}
}

func TestWriteFailureCountsFailedRows(t *testing.T) {
	w := &fakeWriter{retailFail: -1}
	s := newTestStore(w, func(c *Config) {
		c.BatchSize = 10
		c.FlushInterval = 10 * time.Millisecond
		c.Retries = 2
	})
	startStore(t, s)

	s.EnqueueIngest(1, 1, retailMeta(t, 1, 1,
		retailZone{ZoneID: "shelf_1"}, retailZone{ZoneID: "shelf_2"},
		retailZone{ZoneID: "shelf_3"}))

	waitFor(t, "重试耗尽后记失败行数", func() bool { return s.Snapshot().FailedRows == 3 })
	stats := s.Snapshot()
	if stats.WrittenRows != 0 {
		t.Fatalf("written_rows=%d，期望 0", stats.WrittenRows)
	}
	if stats.WriteErrors != 2 {
		t.Fatalf("write_errors=%d，期望 2（Retries=2）", stats.WriteErrors)
	}
}

func TestCloseFlushesPendingBatch(t *testing.T) {
	w := &fakeWriter{}
	// BatchSize 远大于事件行数、FlushInterval 长到不会自然触发：只有 Close 能写出去。
	s := newTestStore(w, func(c *Config) {
		c.BatchSize = 100
		c.FlushInterval = time.Hour
	})
	startStore(t, s)

	s.EnqueueIngest(1, 1, retailMeta(t, 1, 1,
		retailZone{ZoneID: "shelf_1"}, retailZone{ZoneID: "shelf_2"}))
	s.Close()

	rows, _, _, _ := w.snapshot()
	if len(rows) != 2 {
		t.Fatalf("Close 应先刷完待提交批次，实际写入 %d 行", len(rows))
	}
}

func TestPathSessionIDIsTruncatedTo128Runes(t *testing.T) {
	w := &fakeWriter{}
	s := newTestStore(w, func(c *Config) { c.FlushInterval = 10 * time.Millisecond })
	startStore(t, s)

	longID := strings.Repeat("视", 130)
	meta, err := json.Marshal(ingestMessage{Gen: 1, Seq: 1, Paths: []pathRow{{
		SessionID: longID, TrackID: 7, Source: "video",
		Path:      []pathZone{{ZoneID: "shelf_1", ZoneLabel: "货架A", DwellSeconds: 1.5}},
		ZoneCount: 1, TotalDwellSeconds: 1.5,
	}}})
	if err != nil {
		t.Fatalf("构造 paths 事件: %v", err)
	}
	s.EnqueueIngest(1, 1, meta)

	waitFor(t, "等待动线写入", func() bool {
		_, _, paths, _ := w.snapshot()
		return len(paths) == 1
	})
	_, _, paths, _ := w.snapshot()
	if got := len([]rune(paths[0].SessionID)); got != 128 {
		t.Fatalf("session_id 截断后 %d 个字符，期望 128", got)
	}
	var decoded []pathZone
	if err := json.Unmarshal([]byte(paths[0].PathJSON), &decoded); err != nil {
		t.Fatalf("path 列应为合法 JSON: %v", err)
	}
	if len(decoded) != 1 || decoded[0].ZoneLabel != "货架A" {
		t.Fatalf("path JSON 内容不符: %+v", decoded)
	}
	if paths[0].ZoneCount != 1 || paths[0].TotalDwellSeconds != 1.5 {
		t.Fatalf("动线字段不符: %+v", paths[0])
	}
}

func TestMissingOptionalFieldsGetDefaults(t *testing.T) {
	w := &fakeWriter{}
	s := newTestStore(w, func(c *Config) { c.FlushInterval = 10 * time.Millisecond })
	startStore(t, s)

	meta, err := json.Marshal(ingestMessage{
		Gen: 1, Seq: 1,
		Retail: &retailBatch{PeriodKey: "202610171605",
			Zones: []retailZone{{ZoneID: "shelf_1", VisitCount: 2}}},
		Alerts: []alertRow{{AlertType: "anomaly", ZoneID: "shelf_1", Score: 5}},
	})
	if err != nil {
		t.Fatalf("构造事件: %v", err)
	}
	s.EnqueueIngest(1, 1, meta)

	waitFor(t, "等待两张表写入", func() bool {
		rows, alerts, _, _ := w.snapshot()
		return len(rows) == 1 && len(alerts) == 1
	})
	rows, alerts, _, _ := w.snapshot()
	if rows[0].ZoneType != "shelf" || rows[0].Source != "video" || rows[0].SavedAt == "" {
		t.Fatalf("retail 默认值不符: %+v", rows[0])
	}
	if alerts[0].Level != "watch" || alerts[0].CreatedAt == "" {
		t.Fatalf("alert 默认值不符: %+v", alerts[0])
	}
}

func TestInsertSQLShape(t *testing.T) {
	got := insertSQL("t", []string{"a", "b"}, 2)
	want := "INSERT INTO t (a, b) VALUES (?,?),(?,?)"
	if got != want {
		t.Fatalf("insertSQL=%q，期望 %q", got, want)
	}
}
