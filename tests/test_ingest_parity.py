"""benchmark/ingest_parity.py 的离线单测（不依赖 MySQL）。

覆盖对账脚本的纯逻辑：快照归一化、retail_stats 的分钟桶折叠、按自然键/
身份字段配对、数值容差、忽略字段，以及"差异非 0 判不一致"的口径。
真实 MySQL 快照与两轮运行验收在`benchmark/ingest_parity.py`里手工执行。
"""
from datetime import datetime
from decimal import Decimal

import benchmark.ingest_parity as ip


def _retail(zone_id, visit=1, dwell=10.0, heat=1.0, label=None):
    return {
        "zone_id": zone_id,
        "zone_type": "shelf",
        "zone_label": label or zone_id,
        "visit_count": visit,
        "deep_interest_count": 0,
        "total_dwell_seconds": dwell,
        "heat_score": heat,
        "source": "video",
    }


def _alert(reason="A", score=1, frame_id=10):
    return {
        "alert_type": "anomaly",
        "zone_id": "z1",
        "person_id": 1,
        "level": "watch",
        "score": score,
        "reason": reason,
        "frame_id": frame_id,
    }


def _path(track_id=7, zones=("z1", "z2"), dwell=1.5):
    return {
        "track_id": track_id,
        "source": "video",
        "path": [
            {"zone_id": z, "zone_label": z, "dwell_seconds": dwell}
            for z in zones
        ],
        "zone_count": len(zones),
        "total_dwell_seconds": dwell * len(zones),
    }


def _snapshot(label, tables):
    return {
        "tool": "ingest_parity",
        "version": 1,
        "kind": "snapshot",
        "label": label,
        "snapshot_at": "2026-10-17 16:00:00",
        "window": {"since": "2026-10-17 15:00:00",
                   "until": "2026-10-17 16:00:00"},
        "tables": {
            table: {"raw_row_count": len(rows), "row_count": len(rows),
                    "collapsed_by": [], "rows": rows}
            for table, rows in tables.items()
        },
    }


def test_normalize_retail_drops_run_scoped_fields_and_converts_decimal():
    row = {
        "id": 11,
        "zone_id": "z1",
        "zone_type": "shelf",
        "zone_label": "货架A",
        "period_key": "202610171600",
        "period_start": datetime(2026, 10, 17, 16, 0, 0),
        "period_end": datetime(2026, 10, 17, 16, 1, 0),
        "visit_count": 3,
        "deep_interest_count": 1,
        "total_dwell_seconds": Decimal("12.500"),
        "heat_score": Decimal("1.234"),
        "source": "video",
        "created_at": datetime(2026, 10, 17, 16, 0, 30),
    }
    out = ip.normalize_row("retail_stats", row)
    for dropped in ("id", "period_key", "period_start", "period_end", "created_at"):
        assert dropped not in out, dropped
    assert out["total_dwell_seconds"] == 12.5
    assert out["heat_score"] == 1.234
    assert out["visit_count"] == 3


def test_normalize_visit_path_parses_json_string():
    row = {
        "id": 1,
        "session_id": "video_123",
        "track_id": 7,
        "source": "video",
        "path": '[{"zone_id": "z1", "zone_label": "A", "dwell_seconds": 1.5}]',
        "zone_count": 1,
        "total_dwell_seconds": Decimal("1.50"),
        "recorded_at": datetime(2026, 10, 17, 16, 0, 0),
    }
    out = ip.normalize_row("track_visit_paths", row)
    assert out["path"][0]["zone_id"] == "z1"
    assert out["total_dwell_seconds"] == 1.5
    assert "session_id" not in out
    assert "recorded_at" not in out


def test_collapse_retail_keeps_latest_minute_bucket_per_zone():
    rows = [
        {"id": 1, "zone_id": "z1", "period_key": "202610171600",
         "period_start": datetime(2026, 10, 17, 16, 0, 5), "visit_count": 1},
        {"id": 2, "zone_id": "z1", "period_key": "202610171601",
         "period_start": datetime(2026, 10, 17, 16, 1, 5), "visit_count": 4},
        {"id": 3, "zone_id": "z2", "period_key": "202610171600",
         "period_start": datetime(2026, 10, 17, 16, 0, 5), "visit_count": 2},
    ]
    collapsed = ip.collapse_rows("retail_stats", rows)
    by_zone = {row["zone_id"]: row for row in collapsed}
    assert len(collapsed) == 2
    assert by_zone["z1"]["visit_count"] == 4       # 取 period_start 最新的一条
    assert by_zone["z2"]["visit_count"] == 2


def test_snapshot_table_records_raw_and_collapsed_counts():
    rows = [
        {"id": 1, "zone_id": "z1", "period_key": "a",
         "period_start": datetime(2026, 10, 17, 16, 0, 5),
         "period_end": datetime(2026, 10, 17, 16, 0, 6),
         "zone_type": "shelf", "zone_label": "z1", "visit_count": 1,
         "deep_interest_count": 0, "total_dwell_seconds": 1,
         "heat_score": 0, "source": "video",
         "created_at": datetime(2026, 10, 17, 16, 0, 6)},
        {"id": 2, "zone_id": "z1", "period_key": "b",
         "period_start": datetime(2026, 10, 17, 16, 1, 5),
         "period_end": datetime(2026, 10, 17, 16, 1, 6),
         "zone_type": "shelf", "zone_label": "z1", "visit_count": 3,
         "deep_interest_count": 1, "total_dwell_seconds": 5,
         "heat_score": 2, "source": "video",
         "created_at": datetime(2026, 10, 17, 16, 1, 6)},
    ]
    entry = ip.snapshot_table("retail_stats", rows)
    assert entry["raw_row_count"] == 2
    assert entry["row_count"] == 1
    assert entry["rows"][0]["visit_count"] == 3
    # 折叠字段是表名列表，不能把字符串 "zone_id" 拆成单字符列表
    assert entry["collapsed_by"] == ["zone_id"]


def test_compare_identical_snapshots_is_ok():
    tables = {
        "retail_stats": [_retail("z1"), _retail("z2", visit=2)],
        "alert_record": [_alert()],
        "track_visit_paths": [_path()],
    }
    report = ip.compare_snapshots(_snapshot("python", tables),
                                  _snapshot("go", tables))
    assert report["ok"] is True
    assert set(report["tables_compared"]) == set(ip.TABLE_SPECS)
    assert all(res["ok"] for res in report["tables"].values())


def test_compare_missing_and_extra_rows_make_it_fail():
    base = {"retail_stats": [_retail("z1"), _retail("z2")]}
    cand = {"retail_stats": [_retail("z1"), _retail("z3")]}
    report = ip.compare_snapshots(_snapshot("python", base),
                                  _snapshot("go", cand))
    res = report["tables"]["retail_stats"]
    assert report["ok"] is False
    assert res["missing_total"] == 1
    assert res["extra_total"] == 1
    assert res["missing"][0]["zone_id"] == "z2"
    assert res["extra"][0]["zone_id"] == "z3"


def test_compare_numeric_tolerance_boundary():
    base = {"retail_stats": [_retail("z1", dwell=10.0, heat=1.0)]}
    within = {"retail_stats": [_retail("z1", dwell=10.1, heat=1.2)]}
    outside = {"retail_stats": [_retail("z1", dwell=10.3, heat=1.0)]}

    ok_report = ip.compare_snapshots(_snapshot("a", base),
                                     _snapshot("b", within), tolerance=0.2)
    assert ok_report["ok"] is True

    bad_report = ip.compare_snapshots(_snapshot("a", base),
                                      _snapshot("b", outside), tolerance=0.2)
    assert bad_report["ok"] is False
    fields = bad_report["tables"]["retail_stats"]["field_mismatches"][0]["fields"]
    assert fields[0]["field"] == "total_dwell_seconds"
    assert fields[0]["delta"] == 0.3


def test_compare_alert_multiset_matches_ignoring_order():
    base = {"alert_record": [
        _alert(reason="A", score=1),
        _alert(reason="A", score=2),
    ]}
    cand = {"alert_record": [
        _alert(reason="A", score=2),
        _alert(reason="A", score=1),
    ]}
    report = ip.compare_snapshots(_snapshot("a", base), _snapshot("b", cand))
    res = report["tables"]["alert_record"]
    assert res["matched"] == 2
    assert report["ok"] is True


def test_compare_alert_extra_duplicate_is_reported():
    base = {"alert_record": [_alert()]}
    cand = {"alert_record": [_alert(), _alert()]}
    report = ip.compare_snapshots(_snapshot("a", base), _snapshot("b", cand))
    res = report["tables"]["alert_record"]
    assert res["extra_total"] == 1
    assert report["ok"] is False


def test_compare_track_path_nested_dwell_tolerance_and_zone_diff():
    base = {"track_visit_paths": [_path(dwell=1.5)]}
    within = {"track_visit_paths": [_path(dwell=1.6)]}
    assert ip.compare_snapshots(_snapshot("a", base),
                                _snapshot("b", within),
                                tolerance=0.2)["ok"] is True

    changed = {"track_visit_paths": [_path(zones=("z1", "z9"), dwell=1.5)]}
    report = ip.compare_snapshots(_snapshot("a", base),
                                  _snapshot("b", changed))
    fields = report["tables"]["track_visit_paths"]["field_mismatches"][0]["fields"]
    assert any(f["field"] == "path[1].zone_id" for f in fields)
    assert report["ok"] is False


def test_compare_ignore_field_excludes_it():
    base = {"alert_record": [_alert(frame_id=10)]}
    cand = {"alert_record": [_alert(frame_id=99)]}
    report = ip.compare_snapshots(
        _snapshot("a", base), _snapshot("b", cand),
        ignore={"alert_record.frame_id"},
    )
    assert report["ok"] is True
    assert report["ignore"] == ["alert_record.frame_id"]


def test_compare_table_present_in_only_one_snapshot_fails():
    base = {"retail_stats": [_retail("z1")]}
    cand = {"alert_record": [_alert()]}
    report = ip.compare_snapshots(_snapshot("a", base), _snapshot("b", cand))
    assert report["ok"] is False
    assert report["tables_only_in_one"] == ["alert_record", "retail_stats"]


def test_compare_rejects_non_snapshot_input():
    try:
        ip.compare_snapshots({"kind": "diff", "tables": {}}, {"kind": "snapshot"})
    except ValueError:
        return
    raise AssertionError("非 snapshot 输入应抛 ValueError")


def test_ignore_validation_rejects_bad_names():
    for bad in (["retail_stats"], ["nope.x"], ["retail_stats."],
                ["retail_stats.zone_id"], ["alert_record.reason"]):
        try:
            ip._validate_ignore(bad)
        except Exception:
            continue
        raise AssertionError(f"应拒绝: {bad}")
    assert ip._validate_ignore(["alert_record.frame_id"]) == {
        "alert_record.frame_id"}
