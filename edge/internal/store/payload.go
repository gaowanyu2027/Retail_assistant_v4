package store

// kind=4 入库事件的 meta 结构。字段与 vision/ingest.py 的整形结果一一对应，
// 两侧共同以 contracts/ingest_events.json 为判据（见 contract_test.go）。
//
// 这里刻意**不做业务计算**：热度、停留时长、告警评分都由 Python 算好；
// Go 只把行写进三张表，避免同一口径在两处实现。

type ingestMessage struct {
	Gen    int64        `json:"gen"`
	Seq    int64        `json:"seq"`
	Retail *retailBatch `json:"retail"`
	Alerts []alertRow   `json:"alerts"`
	Paths  []pathRow    `json:"paths"`
}

type retailBatch struct {
	PeriodKey string       `json:"period_key"`
	SavedAt   string       `json:"saved_at"`
	Zones     []retailZone `json:"zones"`
}

type retailZone struct {
	ZoneID            string  `json:"zone_id"`
	ZoneType          string  `json:"zone_type"`
	ZoneLabel         string  `json:"zone_label"`
	VisitCount        int     `json:"visit_count"`
	DeepInterestCount int     `json:"deep_interest_count"`
	TotalDwellSeconds float64 `json:"total_dwell_seconds"`
	HeatScore         float64 `json:"heat_score"`
	Source            string  `json:"source"`
}

// retailRow 是 retailBatch 摊平到"一行一 zone"之后的写入单元。
type retailRow struct {
	PeriodKey         string
	ZoneID            string
	ZoneType          string
	ZoneLabel         string
	SavedAt           string
	VisitCount        int
	DeepInterestCount int
	TotalDwellSeconds float64
	HeatScore         float64
	Source            string
}

type alertRow struct {
	AlertType string `json:"alert_type"`
	ZoneID    string `json:"zone_id"`
	PersonID  int64  `json:"person_id"`
	Level     string `json:"level"`
	Score     int    `json:"score"`
	Reason    string `json:"reason"`
	FrameID   int64  `json:"frame_id"`
	CreatedAt string `json:"created_at"`
}

type pathRow struct {
	SessionID         string     `json:"session_id"`
	TrackID           int64      `json:"track_id"`
	Source            string     `json:"source"`
	Path              []pathZone `json:"path"`
	ZoneCount         int        `json:"zone_count"`
	TotalDwellSeconds float64    `json:"total_dwell_seconds"`
	RecordedAt        string     `json:"recorded_at"`
	// PathJSON 是 Path 序列化后的写入形态（track_visit_paths.path 为 JSON 文本）。
	// 由 route 在投递前生成；解码时不从 JSON 读取（输入里只有 path 数组）。
	PathJSON string `json:"-"`
}

type pathZone struct {
	ZoneID       string  `json:"zone_id"`
	ZoneLabel    string  `json:"zone_label"`
	DwellSeconds float64 `json:"dwell_seconds"`
}
