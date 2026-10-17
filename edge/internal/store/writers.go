package store

import (
	"context"
	"database/sql"
	"strings"
)

// SQL 形状与 mysql_db.py 的三个写函数逐列对应：
//
//	save_retail_stats      → INSERT ... ON DUPLICATE KEY UPDATE（幂等，同一分钟同区只留最新）
//	save_alert_record      → 纯 INSERT
//	save_track_visit_path  → 纯 INSERT，path 为 JSON 文本
//
// 唯一的形态差别是"多行 VALUES"：一条语句提交一批，行内容与逐条写入等价。
// 列清单同时是 contracts/ingest_events.json 的 sql_columns 判据（contract_test.go）。

var retailColumns = []string{
	"period_key", "zone_id", "zone_type", "zone_label",
	"period_start", "period_end", "visit_count", "deep_interest_count",
	"total_dwell_seconds", "heat_score", "source",
}

var alertColumns = []string{
	"alert_type", "zone_id", "person_id", "level", "score", "reason",
	"frame_id", "created_at",
}

var pathColumns = []string{
	"session_id", "track_id", "source", "path", "zone_count",
	"total_dwell_seconds",
}

// retailUpsert 与 mysql_db.save_retail_stats 的 ON DUPLICATE KEY UPDATE 一致：
// zone_type 只在插入时使用，不参与更新。
const retailUpsert = " ON DUPLICATE KEY UPDATE" +
	" zone_label=VALUES(zone_label)," +
	" period_start=VALUES(period_start)," +
	" period_end=VALUES(period_end)," +
	" visit_count=VALUES(visit_count)," +
	" deep_interest_count=VALUES(deep_interest_count)," +
	" total_dwell_seconds=VALUES(total_dwell_seconds)," +
	" heat_score=VALUES(heat_score)," +
	" source=VALUES(source)"

type mysqlWriter struct{ db *sql.DB }

func (w *mysqlWriter) execRetail(ctx context.Context, rows []retailRow) error {
	args := make([]any, 0, len(rows)*len(retailColumns))
	for _, r := range rows {
		args = append(args,
			r.PeriodKey, r.ZoneID, r.ZoneType, r.ZoneLabel,
			r.SavedAt, r.SavedAt, r.VisitCount, r.DeepInterestCount,
			r.TotalDwellSeconds, r.HeatScore, r.Source,
		)
	}
	query := insertSQL("retail_stats", retailColumns, len(rows)) + retailUpsert
	_, err := w.db.ExecContext(ctx, query, args...)
	return err
}

func (w *mysqlWriter) execAlerts(ctx context.Context, rows []alertRow) error {
	args := make([]any, 0, len(rows)*len(alertColumns))
	for _, r := range rows {
		args = append(args,
			r.AlertType, r.ZoneID, r.PersonID, r.Level,
			r.Score, r.Reason, r.FrameID, r.CreatedAt,
		)
	}
	query := insertSQL("alert_record", alertColumns, len(rows))
	_, err := w.db.ExecContext(ctx, query, args...)
	return err
}

func (w *mysqlWriter) execPaths(ctx context.Context, rows []pathRow) error {
	args := make([]any, 0, len(rows)*len(pathColumns))
	for _, r := range rows {
		args = append(args,
			r.SessionID, r.TrackID, r.Source, r.PathJSON,
			r.ZoneCount, r.TotalDwellSeconds,
		)
	}
	query := insertSQL("track_visit_paths", pathColumns, len(rows))
	_, err := w.db.ExecContext(ctx, query, args...)
	return err
}

// insertSQL "INSERT INTO <table> (cols) VALUES (?,...),(?,...)"。
func insertSQL(table string, columns []string, rows int) string {
	var b strings.Builder
	b.WriteString("INSERT INTO ")
	b.WriteString(table)
	b.WriteString(" (")
	b.WriteString(strings.Join(columns, ", "))
	b.WriteString(") VALUES ")
	b.WriteString(strings.TrimSuffix(strings.Repeat("("+placeholders(len(columns))+"),", rows), ","))
	return b.String()
}

func placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}
