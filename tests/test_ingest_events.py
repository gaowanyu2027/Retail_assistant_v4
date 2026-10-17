"""S4 入库事件契约的离线单测（不依赖 MySQL / cv2）。

覆盖两件事：
1. `vision.ingest` 整形出的字段与 `contracts/ingest_events.json` 的样例逐字段一致
   （Go 侧 `edge/internal/store/contract_test.go` 读同一份夹具）；
2. 事件日志对 kind=4 的排序、换代与"新读者不补发"语义。
"""
import json
import math
from pathlib import Path

import vision.ingest as ingest
from vision.session import ControlEvent, EventLog, FrameEvent, IngestEvent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((PROJECT_ROOT / "contracts" / "ingest_events.json")
                      .read_text(encoding="utf-8"))


def _sample(name):
    return next(s["meta"] for s in CONTRACT["samples"] if s["name"] == name)


def test_contract_file_has_three_named_samples_and_expected_kind():
    assert CONTRACT["kind"] == 4
    assert [s["name"] for s in CONTRACT["samples"]] == ["retail", "alerts", "paths"]
    for sample in CONTRACT["samples"]:
        meta = sample["meta"]
        assert isinstance(meta["gen"], int) and isinstance(meta["seq"], int)
        json.dumps(meta, ensure_ascii=False, allow_nan=False)


def test_build_retail_payload_matches_contract_fields():
    sample = _sample("retail")
    zones = {"shelf_1": {
        "zone_label": "货架A",
        "visit_count": 3,
        "deep_interest_count": 1,
        "total_dwell_seconds": 12.5,
        "heat_score": 61.2,
        # get_stats 的展示字段不应进入事件
        "hourly_visits": [1, 2, 3],
        "avg_dwell_seconds": 4.1,
    }}
    payload = ingest.build_retail_payload(
        sample["retail"]["period_key"], sample["retail"]["saved_at"], zones)
    assert payload == {"retail": sample["retail"]}


def test_build_alert_rows_match_contract_fields():
    sample = _sample("alerts")
    alert = sample["alerts"][0]
    rows = ingest.build_alert_rows([{
        "zone_visited": [alert["zone_id"]],
        "person_id": alert["person_id"],
        "level": alert["level"],
        "score": alert["score"],
        "reasons": alert["reason"].split("；"),
        "frame_id": alert["frame_id"],
        "timestamp": alert["created_at"],
    }], now_text="2026-10-17 16:09:59")
    assert rows == sample["alerts"]


def test_build_path_row_matches_contract_fields():
    sample = _sample("paths")["paths"][0]
    visited = {
        "shelf_1": {"enter_ts": 100.0, "last_ts": 101.5},
        "shelf_2": {"dwell_frames": 23},            # 23 / 10fps = 2.3
    }
    row = ingest.build_path_row(
        session_id=sample["session_id"], track_id=sample["track_id"],
        visited_zones=visited,
        zone_labels={"shelf_1": "货架A", "shelf_2": "货架B"},
        fps=10.0, now_text=sample["recorded_at"])
    assert row == sample


def test_build_path_row_drops_short_stays_and_needs_two_zones():
    visited = {"shelf_1": {"enter_ts": 0.0, "last_ts": 0.4},   # 过短
               "shelf_2": {"enter_ts": 0.0, "last_ts": 1.0}}
    assert ingest.build_path_row("s", 1, visited, {}, 10.0, "t") is None


def test_build_alert_rows_defaults_timestamp_and_non_finite_numbers():
    rows = ingest.build_alert_rows([{
        "person_id": "7", "score": "5", "reasons": ["a", "b"],
        "zone_visited": [], "timestamp": None,
    }], now_text="2026-10-17 16:00:00")
    assert rows[0]["created_at"] == "2026-10-17 16:00:00"
    assert rows[0]["zone_id"] == ""
    assert rows[0]["reason"] == "a；b"
    retails = ingest.build_retail_payload("k", "t", {
        "z": {"visit_count": 1, "total_dwell_seconds": math.nan,
              "heat_score": math.inf},
    })
    assert retails["retail"]["zones"][0]["total_dwell_seconds"] == 0.0
    assert retails["retail"]["zones"][0]["heat_score"] == 0.0
    json.dumps(retails, allow_nan=False)      # Go 侧 encoding/json 不会拒收


def test_apply_local_routes_to_three_writers():
    calls = []

    class FakeDB:
        @staticmethod
        def save_retail_stats(period_key, zones, **kwargs):
            calls.append(("retail", period_key, list(zones), kwargs))

        @staticmethod
        def save_alert_record(**kwargs):
            calls.append(("alert", kwargs))

        @staticmethod
        def save_track_visit_path(**kwargs):
            calls.append(("path", kwargs))

    ingest.apply_local({
        "retail": {"period_key": "k", "saved_at": "t",
                   "zones": [{"zone_id": "z1", "zone_label": "A"}]},
        "alerts": [{"alert_type": "anomaly", "zone_id": "z1", "person_id": 1,
                    "level": "watch", "score": 1, "reason": "r",
                    "frame_id": 1, "created_at": "t"}],
        "paths": [{"session_id": "s", "track_id": 1, "source": "video",
                   "path": [], "zone_count": 0, "total_dwell_seconds": 0}],
    }, FakeDB)

    assert calls[0][0] == "retail"
    assert calls[0][2] == ["z1"]
    assert calls[0][3] == {"period_start": "t", "period_end": "t",
                           "source": "video"}
    assert calls[1][0] == "alert"
    assert calls[2][0] == "path"


# ==================== 事件日志语义 ====================

def _drain(log, cursor):
    out = []
    while True:
        ev = cursor.next(timeout=0.0)
        if ev is None:
            return out
        out.append(ev)


def test_ingest_is_interleaved_with_frames_and_controls_by_seq():
    log = EventLog()
    gen = log.new_gen()
    cursor = log.cursor()
    log.publish_frame(gen, 1, "{}", b"jpeg")
    log.publish_ingest(gen, {"paths": [{"track_id": 1}]})
    log.publish_control(gen, ["stop"])
    events = _drain(log, cursor)
    assert [type(e) for e in events] == [FrameEvent, IngestEvent, ControlEvent]
    assert events[0].frame_id == 1
    assert events[1].payload["paths"][0]["track_id"] == 1
    assert events[0].seq < events[1].seq < events[2].seq


def test_new_cursor_does_not_replay_old_ingest_events():
    log = EventLog()
    gen = log.new_gen()
    log.publish_ingest(gen, {"alerts": [{"person_id": 1}]})
    cursor = log.cursor()
    assert cursor.next(timeout=0.0) is None


def test_ingest_after_clear_same_gen_is_still_delivered():
    """停止与处理线程的收尾可能交错：本代数据事件不能因 clear 而消失。"""
    log = EventLog()
    gen = log.new_gen()
    cursor = log.cursor()
    log.publish_control(gen, ["stopped"], clear=True)
    log.publish_ingest(gen, {"retail": {"period_key": "k", "saved_at": "t",
                                        "zones": []}})
    events = _drain(log, cursor)
    assert isinstance(events[0], ControlEvent) and events[0].clear is True
    assert isinstance(events[1], IngestEvent)
    assert events[1].payload["retail"]["period_key"] == "k"


def test_ingest_from_previous_generation_is_dropped():
    log = EventLog()
    old_gen = log.new_gen()
    cursor = log.cursor()
    log.new_gen()
    log.publish_ingest(old_gen, {"alerts": [{"person_id": 2}]})
    assert all(not isinstance(e, IngestEvent) for e in _drain(log, cursor))


def test_empty_ingest_payload_is_ignored():
    log = EventLog()
    log.new_gen()
    cursor = log.cursor()
    log.publish_ingest(log.gen, {})
    assert cursor.next(timeout=0.0) is None
