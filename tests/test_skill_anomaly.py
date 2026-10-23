"""异常行为检测：累计计数不被 deque 封顶、店员降分、去重与去重容量。

最关键的回归是 `get_alert_summary()` 的两个口径：

- `total_alerts` = 明细条数，受 `alerts` deque 上限约束（最多 500）；
- `total_alerts_cumulative` = **累计**，不封顶。

历史上突增检测用 `len(alerts)` 算增量，队列一封顶增量恒为 0 → 突增检测永久失效
（实测累计 1200 起后仍报 500）。这里用 520 起告警直接钉住。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    ANOMALY_ALERT_QUEUE_MAX,
    ANOMALY_ALERTED_MAX,
    ANOMALY_STAFF_PENALTY,
)
from cv_engine.tracker import TrackState  # noqa: E402
from skills.skill_anomaly import AnomalySkill  # noqa: E402


class FakeROI:
    """只实现 AnomalySkill 用到的三个查询。"""

    def __init__(self, shelf=("shelf_1", "shelf_2", "shelf_3"),
                 checkout=("checkout_1",), exit_zones=("exit_1",)):
        self._shelf = list(shelf)
        self._checkout = list(checkout)
        self._exit = list(exit_zones)
        self.zone_label = {z: z for z in self._shelf + self._checkout + self._exit}

    def get_shelf_zones(self):
        return list(self._shelf)

    def get_zone_by_type(self, zone_type):
        if zone_type == "checkout":
            return list(self._checkout)
        if zone_type == "exit":
            return list(self._exit)
        return []


def _track(track_id, zones, hit_times=1, is_staff=False):
    t = TrackState(track_id, (0.0, 0.0))
    t.visited_zones = {z: {} for z in zones}
    t.hit_times = hit_times
    t.is_staff = is_staff
    return t


def _weights(**kw):
    base = {"skip_checkout": 0, "no_shelf_visit": 0,
            "avoid_checkout_path": 0, "exit_directly_after_shelf": 0}
    base.update(kw)
    return base


def test_default_scoring_includes_fast_multi_shelf():
    """默认权重：跳过收银 30 + 离开后直奔出口 15 + 快速穿越多货架 10 = 55 → watch。"""
    skill = AnomalySkill(FakeROI())
    track = _track(1, ["shelf_1", "shelf_2", "shelf_3", "exit_1"], hit_times=1)
    out = skill.process([track], frame_id=1, fps=30)
    assert track.anomaly_score == 55
    assert len(out["new_alerts"]) == 1
    assert out["new_alerts"][0]["level"] == "watch"


def test_staff_penalty_can_bring_track_below_watch():
    skill = AnomalySkill(
        FakeROI(),
        l1_weights=_weights(skip_checkout=70),
        l3_weights={"fast_multi_shelf": 0},
    )
    customer = _track(1, ["shelf_1", "exit_1"])
    staff = _track(2, ["shelf_1", "exit_1"], is_staff=True)

    out = skill.process([customer, staff], frame_id=1, fps=30)

    assert customer.anomaly_score == 70
    assert staff.anomaly_score == 70 - ANOMALY_STAFF_PENALTY == 30
    assert [a["person_id"] for a in out["new_alerts"]] == [1], "店员降分后不应告警"
    assert out["new_alerts"][0]["level"] == "high"


def test_same_track_alerts_only_once_but_stays_suspicious():
    skill = AnomalySkill(FakeROI(), alert_threshold_watch=10,
                         alert_threshold_high=100,
                         l1_weights=_weights(skip_checkout=45),
                         l3_weights={"fast_multi_shelf": 0})
    track = _track(9, ["shelf_1", "exit_1"])

    first = skill.process([track], frame_id=1, fps=30)
    second = skill.process([track], frame_id=2, fps=30)

    assert len(first["new_alerts"]) == 1
    assert second["new_alerts"] == [], "同一轨迹不重复告警"
    assert second["active_suspicious_tracks"] == [{"track_id": 9, "score": 45}]
    assert skill.get_alert_summary()["total_alerts"] == 1


def test_cumulative_counts_exceed_deque_cap():
    skill = AnomalySkill(FakeROI(), alert_threshold_watch=10,
                         alert_threshold_high=100,
                         l1_weights=_weights(skip_checkout=45),
                         l3_weights={"fast_multi_shelf": 0})
    tracks = [_track(tid, ["shelf_1", "exit_1"]) for tid in range(1, 521)]

    out = skill.process(tracks, frame_id=1, fps=30)
    summary = skill.get_alert_summary()

    assert len(out["new_alerts"]) == 520
    assert summary["total_alerts"] == ANOMALY_ALERT_QUEUE_MAX == 500
    assert summary["total_alerts_cumulative"] == 520
    assert summary["watch_count"] == 500
    assert summary["high_risk_count_cumulative"] == 0


def test_alerted_track_memory_is_bounded():
    skill = AnomalySkill(FakeROI(), alert_threshold_watch=10,
                         alert_threshold_high=100,
                         l1_weights=_weights(skip_checkout=45),
                         l3_weights={"fast_multi_shelf": 0})
    tracks = [_track(tid, ["shelf_1", "exit_1"]) for tid in range(1, ANOMALY_ALERTED_MAX + 6)]

    skill.process(tracks, frame_id=1, fps=30)

    assert len(skill._alerted_tracks) == ANOMALY_ALERTED_MAX
    assert 1 not in skill._alerted_tracks, "最旧的已告警轨迹被淘汰"
    assert ANOMALY_ALERTED_MAX + 5 in skill._alerted_tracks


def test_get_alerts_filters_and_reset_clears_everything():
    skill = AnomalySkill(FakeROI(), alert_threshold_watch=10,
                         alert_threshold_high=100,
                         l1_weights=_weights(skip_checkout=45),
                         l3_weights={"fast_multi_shelf": 0})
    skill.process([_track(1, ["shelf_1", "exit_1"])], frame_id=1, fps=30,
                  timestamp="2026-10-23 15:00:00")

    alerts = skill.get_alerts()
    assert len(alerts) == 1 and alerts[0]["timestamp"] == "2026-10-23 15:00:00"
    assert skill.get_alerts(level="high") == []
    assert skill.get_alerts(min_score=46) == []

    skill.reset()
    summary = skill.get_alert_summary()
    assert summary["total_alerts"] == 0
    assert summary["total_alerts_cumulative"] == 0
    assert summary["high_risk"] == [] and summary["watch_list"] == []
