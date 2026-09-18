"""访客数口径测试（台账 D17：一个人被重复计成多个访客）。

原实现里 `get_stats()` 有两个字段算**同一个值**：

    "total_visitors": sum(z.visit_count for z in zone_stats.values())
    "total_visits":   sum(z.visit_count for z in zone_stats.values())

于是"总访客"实际是"区域到访次数合计" —— 同一个人逛 A→B→C 三个货架会被算成 **3 个访客**。
现在两者分开：访客按人去重，区域到访次数保留（区域热度排序要用它）。

用假轨迹 + 假 ROI 喂帧，纯离线。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from skills.skill_popularity import PopularitySkill  # noqa: E402


class FakeROI:
    """zone_id 直接用"坐标点"，方便让轨迹在多个区域间移动。"""

    zone_label = {"A": "货架A", "B": "货架B", "C": "货架C"}

    def get_zone(self, point):
        return point[0] if point and point[0] in self.zone_label else None

    def is_shelf_zone(self, zone_id):
        return zone_id in self.zone_label

    def get_shelf_zones(self):
        return set(self.zone_label)

    def is_exit_zone(self, zone_id):
        return False


class FakeTrack:
    def __init__(self, tid, zone):
        self.track_id = tid
        self.center = (zone, 0)
        self.hit_times = 5
        self.is_staff = False
        self.visited_zones = {}
        self.emotion = "unknown"
        self.emotion_conf = 0.0


def _feed(skill, tracks, frame_id, ts):
    skill.process(tracks, frame_id=frame_id, fps=10.0, timestamp=ts, current_hour="2026010112")


def test_one_person_three_zones_counts_as_one_visitor():
    """核心回归：一个人逛三个货架 → 访客 1 人、区域到访 3 次。"""
    skill = PopularitySkill(FakeROI())
    t = FakeTrack(7, "A")
    _feed(skill, [t], 1, 0.0)
    t.center = ("B", 0)
    _feed(skill, [t], 2, 0.1)
    t.center = ("C", 0)
    _feed(skill, [t], 3, 0.2)

    st = skill.get_stats()
    assert st["total_visits"] == 3, f"区域到访次数应为 3，实际 {st['total_visits']}"
    assert st["total_visitors"] == 1, f"同一人应只算 1 个访客，实际 {st['total_visitors']}"
    assert st["total_visitors"] != st["total_visits"], "两者口径必须不同（原实现是同一个值）"


def test_two_people_count_as_two_visitors():
    skill = PopularitySkill(FakeROI())
    _feed(skill, [FakeTrack(1, "A"), FakeTrack(2, "B")], 1, 0.0)
    st = skill.get_stats()
    assert st["total_visitors"] == 2 and st["total_visits"] == 2, st


def test_repeat_entries_by_same_track_do_not_double_count():
    """同一条轨迹在同一区域反复进出，也只算 1 次到访 / 1 个访客。"""
    skill = PopularitySkill(FakeROI())
    t = FakeTrack(3, "A")
    for i in range(3):
        _feed(skill, [t], i + 1, i * 0.1)
    st = skill.get_stats()
    assert st["total_visits"] == 1 and st["total_visitors"] == 1, st


def test_low_hit_tracks_are_ignored():
    """命中帧数不足（误检）的轨迹不计数。"""
    skill = PopularitySkill(FakeROI())
    t = FakeTrack(9, "A")
    t.hit_times = 1
    _feed(skill, [t], 1, 0.0)
    st = skill.get_stats()
    assert st["total_visitors"] == 0 and st["total_visits"] == 0, st


def test_reset_clears_unique_visitors():
    """换视频源时 reset 必须把唯一访客集合也清掉，否则新会话继承旧计数。"""
    skill = PopularitySkill(FakeROI())
    _feed(skill, [FakeTrack(5, "A")], 1, 0.0)
    assert skill.get_stats()["total_visitors"] == 1
    skill.reset()
    st = skill.get_stats()
    assert st["total_visitors"] == 0 and st["total_visits"] == 0, st


def test_zone_level_numbers_still_available():
    """区域级数字（热度排序要用）不受影响：每个区域仍各自记到访次数。"""
    skill = PopularitySkill(FakeROI())
    t = FakeTrack(11, "A")
    _feed(skill, [t], 1, 0.0)
    t.center = ("B", 0)
    _feed(skill, [t], 2, 0.1)
    zones = skill.get_stats()["zones"]
    assert zones["A"]["visit_count"] == 1 and zones["B"]["visit_count"] == 1, zones
    assert zones["C"]["visit_count"] == 0, zones
