"""
SKII-2：异常行为检测（三级评分 + 店员过滤）

店员轨迹自动降低异常评分（店员频繁进出仓库/货架是正常工作行为）
"""
from dataclasses import dataclass, field
from typing import Any
from collections import OrderedDict, defaultdict, deque

from cv_engine.tracker import TrackState
from cv_engine.roi_manager import ROIManager
from config.settings import (
    ANOMALY_ALERT_QUEUE_MAX,
    ANOMALY_ALERT_THRESHOLD_HIGH,
    ANOMALY_ALERT_THRESHOLD_WATCH,
    ANOMALY_ALERTED_MAX,
    ANOMALY_FAST_MULTI_SHELF_COUNT,
    ANOMALY_FAST_MULTI_SHELF_HITS,
    ANOMALY_GLOBAL_LOG_MAX,
    ANOMALY_L1_WEIGHTS,
    ANOMALY_L2_WEIGHTS,
    ANOMALY_L3_WEIGHTS,
    ANOMALY_NO_SHELF_HIT_THRESHOLD,
    ANOMALY_STAFF_PENALTY,
)


@dataclass
class AnomalyAlert:
    person_id: int
    frame_id: int
    score: int
    level: str
    reasons: list[str]
    zone_visited: list[str]
    timestamp: str = ""


class AnomalySkill:
    """异常行为检测（店员过滤版）"""

    def __init__(
        self,
        roi_manager: ROIManager,
        alert_threshold_watch: int = ANOMALY_ALERT_THRESHOLD_WATCH,
        alert_threshold_high: int = ANOMALY_ALERT_THRESHOLD_HIGH,
        l1_weights: dict[str, int] | None = None,
        l2_weights: dict[str, int] | None = None,
        l3_weights: dict[str, int] | None = None,
    ):
        self.roi_manager = roi_manager
        self.alert_threshold_watch = alert_threshold_watch
        self.alert_threshold_high = alert_threshold_high

        self.l1_weights = l1_weights or ANOMALY_L1_WEIGHTS
        self.l2_weights = l2_weights or ANOMALY_L2_WEIGHTS
        self.l3_weights = l3_weights or ANOMALY_L3_WEIGHTS

        self.alerts: deque[AnomalyAlert] = deque(maxlen=ANOMALY_ALERT_QUEUE_MAX)
        # 累计告警计数（**不封顶**，供"异常突增"判定使用）。
        #
        # ⚠ 为什么必须有它：`alerts` 是 deque，超过 ANOMALY_ALERT_QUEUE_MAX 会丢弃最旧的，
        # 于是 `len(self.alerts)` 永远停在 500。而突增检测靠的是"两次观测的**增量**"，
        # 队列一封顶增量就恒为 0 → **突增检测永久失效**。
        # 实测：累计产生 1200 起后，total_alerts / high_risk_count 仍报 500，
        # 基线也停在 500，之后无论真实新增多少都不再触发。
        self._total_count: int = 0
        self._high_count: int = 0
        self._alerted_tracks: OrderedDict[int, None] = OrderedDict()
        self._alerted_max = ANOMALY_ALERTED_MAX
        self._global_log: deque[dict] = deque(maxlen=ANOMALY_GLOBAL_LOG_MAX)

    # ========== L1 轨迹逻辑 ==========

    def _check_trajectory_logic(
        self,
        track: TrackState,
        frame_id: int,
        shelf_zones: set[str],
        checkout_zones: set[str],
        exit_zones: set[str],
    ) -> tuple[int, list[str]]:
        score = 0
        reasons = []

        visited_zones = track.visited_zones
        visited_shelves = any(z in shelf_zones for z in visited_zones)
        visited_checkout = any(z in checkout_zones for z in visited_zones)
        visited_exit = any(z in exit_zones for z in visited_zones)

        if visited_shelves and not visited_checkout and (visited_exit or track.is_near_exit):
            score += self.l1_weights["skip_checkout"]
            reasons.append("访问货架区后未经过收银台区域")

        if (
            not visited_shelves and not visited_checkout
            and (visited_exit or track.is_near_exit)
            and track.hit_times > ANOMALY_NO_SHELF_HIT_THRESHOLD
        ):
            score += self.l1_weights["no_shelf_visit"]
            reasons.append("未浏览货架直接离开")

        if visited_shelves and not visited_checkout and visited_exit:
            score += self.l1_weights["exit_directly_after_shelf"]
            reasons.append("离开货架区后直接走向出口")

        return score, reasons

    # ========== L2 / L3（预留） ==========

    def _check_pose_anomaly(self, track: TrackState, frame_id: int) -> tuple[int, list[str]]:
        return 0, []

    def _check_behavior_pattern(
        self,
        track: TrackState,
        frame_id: int,
        shelf_zones: set[str],
    ) -> tuple[int, list[str]]:
        score = 0
        reasons = []
        visited_count = sum(1 for z in shelf_zones if z in track.visited_zones)
        if (
            visited_count >= ANOMALY_FAST_MULTI_SHELF_COUNT
            and track.hit_times < ANOMALY_FAST_MULTI_SHELF_HITS
        ):
            score += self.l3_weights["fast_multi_shelf"]
            reasons.append("短时间内快速穿越多个货架区域")
        return score, reasons

    # ========== 主处理 ==========

    def process(
        self, tracks: list[TrackState], frame_id: int, fps: float, timestamp: str = ""
    ) -> dict[str, Any]:
        new_alerts = []
        active_suspicious = []
        shelf_zones = set(self.roi_manager.get_shelf_zones())
        checkout_zones = set(self.roi_manager.get_zone_by_type("checkout"))
        exit_zones = set(self.roi_manager.get_zone_by_type("exit"))

        for track in tracks:
            if track.track_id in self._alerted_tracks:
                if track.anomaly_score >= self.alert_threshold_watch:
                    active_suspicious.append({
                        "track_id": track.track_id,
                        "score": track.anomaly_score,
                    })
                continue

            total_score = 0
            all_reasons = []

            l1_score, l1_reasons = self._check_trajectory_logic(
                track, frame_id, shelf_zones, checkout_zones, exit_zones
            )
            total_score += l1_score
            all_reasons.extend(l1_reasons)

            l2_score, l2_reasons = self._check_pose_anomaly(track, frame_id)
            total_score += l2_score
            all_reasons.extend(l2_reasons)

            l3_score, l3_reasons = self._check_behavior_pattern(
                track, frame_id, shelf_zones
            )
            total_score += l3_score
            all_reasons.extend(l3_reasons)

            # ==== 店员过滤：自动降分 ====
            if track.is_staff:
                total_score = max(0, total_score - ANOMALY_STAFF_PENALTY)
                all_reasons.append("疑似店员，评分降低")

            track.anomaly_score = total_score

            if total_score >= self.alert_threshold_watch:
                level = "high" if total_score >= self.alert_threshold_high else "watch"
                alert = AnomalyAlert(
                    person_id=track.track_id, frame_id=frame_id,
                    score=total_score, level=level,
                    reasons=all_reasons,
                    zone_visited=list(track.visited_zones.keys()),
                    timestamp=timestamp,
                )
                self.alerts.append(alert)
                # 累计计数（不封顶）——队列只保存明细，增量判定看这个
                self._total_count += 1
                if level == "high":
                    self._high_count += 1
                if track.track_id not in self._alerted_tracks:
                    self._alerted_tracks[track.track_id] = None
                    if len(self._alerted_tracks) > self._alerted_max:
                        self._alerted_tracks.popitem(last=False)
                new_alerts.append(alert)

                active_suspicious.append({
                    "track_id": track.track_id, "score": total_score,
                })

        self._global_log.append({
            "frame_id": frame_id, "track_count": len(tracks),
            "new_alerts": len(new_alerts),
        })

        return {
            "new_alerts": [
                {"person_id": a.person_id, "frame_id": a.frame_id,
                 "score": a.score, "level": a.level,
                 "reasons": a.reasons, "zone_visited": a.zone_visited,
                 "timestamp": a.timestamp}
                for a in new_alerts
            ],
            "active_suspicious_tracks": active_suspicious,
        }

    def get_alerts(self, level: str | None = None, min_score: int | None = None) -> list[dict]:
        result = []
        for alert in self.alerts:
            if level and alert.level != level:
                continue
            if min_score and alert.score < min_score:
                continue
            result.append({
                "person_id": alert.person_id, "frame_id": alert.frame_id,
                "score": alert.score, "level": alert.level,
                "reasons": alert.reasons, "zone_visited": alert.zone_visited,
                "timestamp": alert.timestamp,
            })
        return result

    def get_alert_summary(self) -> dict:
        total = 0
        high = []
        watch = []
        for alert in self.alerts:
            total += 1
            item = {
                "person_id": alert.person_id,
                "frame_id": alert.frame_id,
                "score": alert.score,
                "level": alert.level,
                "reasons": alert.reasons,
                "zone_visited": alert.zone_visited,
                "timestamp": alert.timestamp,
            }
            if alert.level == "high":
                high.append(item)
            elif alert.level == "watch":
                watch.append(item)
        return {
            "total_alerts": total,                    # 明细条数（受队列上限约束，最多 500）
            "high_risk_count": len(high),
            "watch_count": len(watch),
            # 累计口径：**不封顶**，供"异常突增"增量判定使用（见 __init__ 的说明）
            "total_alerts_cumulative": self._total_count,
            "high_risk_count_cumulative": self._high_count,
            "high_risk": high,
            "watch_list": watch,
        }

    def reset(self):
        self.alerts.clear()
        self._alerted_tracks.clear()
        self._global_log.clear()
        self._total_count = 0
        self._high_count = 0
