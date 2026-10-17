"""视频派生表的入库事件（S4 契约）：数据整形 + 本地写入。

三张表（`retail_stats` / `alert_record` / `track_visit_paths`）的**视频派生**
写入有两个执行者，由 `GO_OWNS_INGEST_WRITES` 选择：

- `0`（默认）：Python 进程直接写 MySQL，行为与迁移前一致；
- `1`：Python 把同一批数据作为**上行流的 kind=4 事件**发给 Go edge，由
  `edge/internal/store` 落库；本进程不再写这三张表。

本模块只做**纯数据整形**，不 import cv2 / MySQL / torch，因此能在最小依赖的
测试环境里覆盖契约字段；真正的写库在 `apply_local()` 里延迟使用传入的模块。

契约的唯一判据是 `contracts/ingest_events.json`：Python 侧
`tests/test_ingest_events.py` 与 Go 侧 `edge/internal/store/contract_test.go`
都读它，任何一侧改了字段都会红。

字段约定（与 mysql_db 的写函数一一对应）：

    retail:  period_key, saved_at, zones[]
             zone: zone_id, zone_type, zone_label, visit_count,
                   deep_interest_count, total_dwell_seconds, heat_score, source
    alert:   alert_type, zone_id, person_id, level, score, reason, frame_id, created_at
    path:    session_id, track_id, source, path[], zone_count,
             total_dwell_seconds, recorded_at
             path 元素: zone_id, zone_label, dwell_seconds

**为什么把 zone_type/source 也放进事件**：Go 只做持久化，不复刻业务常量；
否则 "shelf"/"video" 这类取值会变成两处定义。
"""
from __future__ import annotations

import math


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    """转 float 并把 NaN/Inf 归零：JSON 不允许 NaN，Go 的 encoding/json 会拒收。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def build_retail_payload(period_key: str, saved_at: str, zones: dict) -> dict:
    """把 `skill_popularity.get_stats()["zones"]` 整形成 retail 事件。

    只保留三张表实际需要的字段：get_stats 里的 hourly_* / avg_dwell 等是
    接口展示用的，不进库，也就不进事件（避免契约随展示字段漂移）。
    """
    rows = []
    for zone_id, zone in (zones or {}).items():
        rows.append({
            "zone_id": str(zone_id),
            "zone_type": "shelf",
            "zone_label": str(zone.get("zone_label", zone_id)),
            "visit_count": _as_int(zone.get("visit_count", 0)),
            "deep_interest_count": _as_int(zone.get("deep_interest_count", 0)),
            "total_dwell_seconds": _as_float(zone.get("total_dwell_seconds", 0)),
            "heat_score": _as_float(zone.get("heat_score", 0)),
            "source": "video",
        })
    if not rows:
        return {}
    return {"retail": {
        "period_key": str(period_key),
        "saved_at": str(saved_at),
        "zones": rows,
    }}


def build_alert_rows(new_alerts: list[dict], now_text: str) -> list[dict]:
    """把 anomaly skill 的 `new_alerts` 整形成 alert 事件行。"""
    rows = []
    for alert in new_alerts or []:
        visited = alert.get("zone_visited") or [""]
        rows.append({
            "alert_type": "anomaly",
            "zone_id": str(visited[0] if visited else ""),
            "person_id": _as_int(alert.get("person_id", 0)),
            "level": str(alert.get("level", "watch") or "watch"),
            "score": _as_int(alert.get("score", 0)),
            "reason": "；".join(str(r) for r in (alert.get("reasons") or [])),
            "frame_id": _as_int(alert.get("frame_id", 0)),
            "created_at": str(alert.get("timestamp") or now_text),
        })
    return rows


def build_path_row(session_id: str, track_id: int, visited_zones: dict,
                   zone_labels: dict, fps: float, now_text: str,
                   min_dwell_seconds: float = 0.5,
                   min_zones: int = 2) -> dict | None:
    """把一条退役轨迹整形成 path 事件行；不足两条有效区域时返回 None。

    停留时长口径与迁移前完全一致：优先 `last_ts - enter_ts`，否则
    `dwell_frames / fps`；只保留 >0.5s 的区域。
    """
    path: list[dict] = []
    total_dwell = 0.0
    for zone_id, rec in (visited_zones or {}).items():
        dwell = 0.0
        if rec.get("enter_ts") is not None and rec.get("last_ts") is not None:
            dwell = max(rec["last_ts"] - rec["enter_ts"], 0.0)
        elif rec.get("dwell_frames"):
            dwell = rec["dwell_frames"] / max(_as_float(fps, 1.0), 1.0)
        if dwell > min_dwell_seconds:
            path.append({
                "zone_id": str(zone_id),
                "zone_label": str((zone_labels or {}).get(zone_id, zone_id)),
                "dwell_seconds": round(dwell, 1),
            })
            total_dwell += dwell
    if len(path) < min_zones:
        return None
    return {
        "session_id": str(session_id),
        "track_id": _as_int(track_id),
        "source": "video",
        "path": path,
        "zone_count": len(path),
        "total_dwell_seconds": round(total_dwell, 1),
        "recorded_at": str(now_text),
    }


def apply_local(payload: dict, db) -> None:
    """把一批事件写到本地 MySQL（`GO_OWNS_INGEST_WRITES=0` 的路径）。

    `db` 是需要 `save_retail_stats` / `save_alert_record` /
    `save_track_visit_path` 三个函数的模块（生产传 mysql_db，测试传假模块）。
    """
    retail = payload.get("retail")
    if retail:
        zones = {row["zone_id"]: row for row in retail["zones"]}
        db.save_retail_stats(
            retail["period_key"], zones,
            period_start=retail["saved_at"], period_end=retail["saved_at"],
            source="video",
        )
    for alert in payload.get("alerts") or []:
        db.save_alert_record(
            alert_type=alert["alert_type"],
            zone_id=alert["zone_id"],
            person_id=alert["person_id"],
            level=alert["level"],
            score=alert["score"],
            reason=alert["reason"],
            frame_id=alert["frame_id"],
            created_at=alert["created_at"],
        )
    for path in payload.get("paths") or []:
        db.save_track_visit_path(
            session_id=path["session_id"],
            track_id=path["track_id"],
            path=path["path"],
            source=path["source"],
            zone_count=path["zone_count"],
            total_dwell_seconds=path["total_dwell_seconds"],
        )
