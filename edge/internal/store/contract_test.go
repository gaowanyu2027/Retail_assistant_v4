package store

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"testing"
	"time"
)

// 契约夹具由 Python 侧（vision/ingest.py）与 Go 侧共同消费：
// 谁改了字段，这里最先失败。

type contractFixture struct {
	Schema  int    `json:"schema"`
	Kind    int    `json:"kind"`
	Source  string `json:"source"`
	Samples []struct {
		Name string          `json:"name"`
		Meta json.RawMessage `json:"meta"`
	} `json:"samples"`
	SQLColumns map[string][]string `json:"sql_columns"`
}

func loadContract(t *testing.T) contractFixture {
	t.Helper()
	path := filepath.Join("..", "..", "..", "contracts", "ingest_events.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取契约夹具 %s: %v", path, err)
	}
	var fx contractFixture
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatalf("解析契约夹具: %v", err)
	}
	return fx
}

func TestContractEnvelopeKindAndColumns(t *testing.T) {
	fx := loadContract(t)
	// 与 edge/internal/video/envelope.go 的 envelopeKindIngest 一致。
	if fx.Schema != 1 || fx.Kind != 4 {
		t.Fatalf("schema=%d kind=%d，期望 1/4", fx.Schema, fx.Kind)
	}
	cases := []struct {
		table   string
		columns []string
	}{
		{"retail_stats", retailColumns},
		{"alert_record", alertColumns},
		{"track_visit_paths", pathColumns},
	}
	for _, c := range cases {
		if !slices.Equal(c.columns, fx.SQLColumns[c.table]) {
			t.Fatalf("%s 列清单与契约不一致:\n Go   = %v\n 夹具 = %v",
				c.table, c.columns, fx.SQLColumns[c.table])
		}
	}
	if timeLayout != "2006-01-02 15:04:05" {
		t.Fatalf("timeLayout=%q，与契约的 datetime 文本格式不一致", timeLayout)
	}
}

func TestContractSamplesProduceExpectedRows(t *testing.T) {
	fx := loadContract(t)

	var msgs []ingestMessage
	names := map[string]bool{}
	for _, sample := range fx.Samples {
		var msg ingestMessage
		if err := json.Unmarshal(sample.Meta, &msg); err != nil {
			t.Fatalf("样例 %q 无法解析为 ingestMessage: %v", sample.Name, err)
		}
		if msg.Gen <= 0 || msg.Seq <= 0 {
			t.Fatalf("样例 %q 缺少 gen/seq: %+v", sample.Name, msg)
		}
		names[sample.Name] = true
		msgs = append(msgs, msg)
	}
	for _, want := range []string{"retail", "alerts", "paths"} {
		if !names[want] {
			t.Fatalf("契约缺少 %q 样例", want)
		}
	}
	// 按 seq 顺序投递（去重水位要求严格递增）。
	sort.Slice(msgs, func(i, j int) bool { return msgs[i].Seq < msgs[j].Seq })

	w := &fakeWriter{}
	s := newTestStore(w, func(c *Config) { c.FlushInterval = 10 * time.Millisecond })
	startStore(t, s)
	for _, msg := range msgs {
		raw, err := json.Marshal(msg)
		if err != nil {
			t.Fatalf("重新序列化样例: %v", err)
		}
		s.EnqueueIngest(msg.Gen, msg.Seq, raw)
	}

	waitFor(t, "三张表各写入样例行", func() bool {
		rows, alerts, paths, _ := w.snapshot()
		return len(rows) == 1 && len(alerts) == 1 && len(paths) == 1
	})
	rows, alerts, paths, _ := w.snapshot()

	wantRetail := retailRow{
		PeriodKey: "202610171605", ZoneID: "shelf_1", ZoneType: "shelf",
		ZoneLabel: "货架A", SavedAt: "2026-10-17 16:05:00",
		VisitCount: 3, DeepInterestCount: 1,
		TotalDwellSeconds: 12.5, HeatScore: 61.2, Source: "video",
	}
	if rows[0] != wantRetail {
		t.Fatalf("retail 行与契约不符:\n got = %+v\nwant = %+v", rows[0], wantRetail)
	}

	wantAlert := alertRow{
		AlertType: "anomaly", ZoneID: "shelf_1", PersonID: 7, Level: "watch",
		Score: 5, Reason: "在货架区长时间徘徊；多次折返", FrameID: 123,
		CreatedAt: "2026-10-17 16:05:01",
	}
	if alerts[0] != wantAlert {
		t.Fatalf("alert 行与契约不符:\n got = %+v\nwant = %+v", alerts[0], wantAlert)
	}

	if paths[0].SessionID != "video_1760697900" || paths[0].TrackID != 7 ||
		paths[0].Source != "video" || paths[0].ZoneCount != 2 ||
		paths[0].TotalDwellSeconds != 3.8 {
		t.Fatalf("path 行与契约不符: %+v", paths[0])
	}
	var decoded []pathZone
	if err := json.Unmarshal([]byte(paths[0].PathJSON), &decoded); err != nil {
		t.Fatalf("path 列不是合法 JSON: %v", err)
	}
	wantPath := []pathZone{
		{ZoneID: "shelf_1", ZoneLabel: "货架A", DwellSeconds: 1.5},
		{ZoneID: "shelf_2", ZoneLabel: "货架B", DwellSeconds: 2.3},
	}
	if !slices.Equal(decoded, wantPath) {
		t.Fatalf("path 列内容不符:\n got = %+v\nwant = %+v", decoded, wantPath)
	}
}
