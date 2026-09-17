"""新分析模块口径测试（台账 A17 / A18）。

被测的两个错误都是"单位/口径"错，不是崩溃，所以只能靠**断言数字**发现：
- A18 门口客流"离开判定"写死 30 **帧** → fps≠30 时秒数整体错（fps=5 时慢 6 倍）；
- A17 空货架"到访数"是**自创建以来按帧累计** → 一个人站 30 秒算 30 次到访，
  且没有时间窗、观察不足时也直接报"需补货"。

手法：假 ROI + 假轨迹喂帧，纯离线（不需要摄像头/模型/数据库）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    EMPTY_SHELF_MIN_OBSERVE_SECONDS,
    EMPTY_SHELF_WINDOW_SECONDS,
    FOOTFALL_EXIT_MISSING_SECONDS,
)
from skills.skill_new_modules import _EmptyShelfModule, _FootfallModule  # noqa: E402

SHELF = "shelf_a"


class FakeROI:
    """最小 ROI 桩：只实现模块实际用到的方法。"""

    def __init__(self, shelf_zones=(SHELF,)):
        self.zone_label = {z: f"货架-{z}" for z in shelf_zones}
        self._shelf = set(shelf_zones)

    def get_shelf_zones(self):
        return set(self._shelf)

    def get_zone(self, center):
        # center 直接当区域名用；None = 不在任何区域
        return center


def _tracks(*tids):
    """构造有效轨迹：hit_times 必须 > 2（模块用它过滤误检）。"""
    return [SimpleNamespace(track_id=t, center=SHELF, hit_times=5) for t in tids]


# ---------------- A18：离开判定按秒，不按帧 ----------------

def test_footfall_exit_threshold_is_seconds():
    """fps=5 时也必须在 FOOTFALL_EXIT_MISSING_SECONDS 秒后计外出（而不是 6 秒后）。"""
    mod = _FootfallModule(FakeROI())
    mod.process(_tracks(7), frame_id=1, fps=5, timestamp=0.0)
    assert mod.get_stats()["total_entry"] == 1

    t = 0.0
    step = FOOTFALL_EXIT_MISSING_SECONDS / 5.0        # 分 5 步走完阈值
    for i in range(4):                                # 还差一步 → 不能算离店
        t += step
        mod.process([], frame_id=2 + i, fps=5, timestamp=t)
        assert mod.get_stats()["total_exit"] == 0, f"t={t:.2f}s 就判离店，阈值没生效"
    t += step
    mod.process([], frame_id=6, fps=5, timestamp=t)   # 达到阈值 → 计一次外出
    assert mod.get_stats()["total_exit"] == 1


def test_footfall_result_is_fps_independent():
    """同一串真实时间戳，fps=5 / 30 / 60 的结论必须一致（这就是修复点）。"""
    outs = []
    for fps in (5, 30, 60):
        mod = _FootfallModule(FakeROI())
        mod.process(_tracks(1), frame_id=1, fps=fps, timestamp=0.0)
        mod.process([], frame_id=2, fps=fps, timestamp=FOOTFALL_EXIT_MISSING_SECONDS - 0.01)
        before = mod.get_stats()["total_exit"]
        mod.process([], frame_id=3, fps=fps, timestamp=FOOTFALL_EXIT_MISSING_SECONDS + 0.01)
        outs.append((before, mod.get_stats()["total_exit"]))
    assert outs == [(0, 1)] * 3, f"不同 fps 结论不一致: {outs}"


def test_footfall_reappear_cancels_exit():
    """轨迹只是短暂未出现（未到阈值）又回来了 → 不外出、也不重复计进店。"""
    mod = _FootfallModule(FakeROI())
    th = FOOTFALL_EXIT_MISSING_SECONDS
    mod.process(_tracks(3), frame_id=1, fps=5, timestamp=0.0)
    mod.process([], frame_id=2, fps=5, timestamp=th * 0.5)     # 缺席 0.5 个阈值
    mod.process(_tracks(3), frame_id=3, fps=5, timestamp=th * 0.8)     # 回来了 → 计时归零
    mod.process([], frame_id=4, fps=5, timestamp=th * 1.2)             # 距上次出现 0.4
    mod.process([], frame_id=5, fps=5, timestamp=th * 1.6)             # 距上次出现 0.8
    st = mod.get_stats()
    assert st["total_exit"] == 0, f"未到阈值就算离店了: {st}"
    assert st["total_entry"] == 1, "同一轨迹不应重复计进店"
    # 再缺席到超过阈值 → 这一次才该计外出
    mod.process([], frame_id=6, fps=5, timestamp=th * 1.9)
    assert mod.get_stats()["total_exit"] == 1


def test_footfall_falls_back_to_frames_without_timestamp():
    """管线没给 timestamp 时才退化用 帧号/fps —— fps=5 时 30 帧不再是阈值。"""
    mod = _FootfallModule(FakeROI())
    mod.process(_tracks(9), frame_id=1, fps=5)          # 无 timestamp → t=1/5=0.2s
    for fid in range(2, 7):                             # 到第 6 帧 (=1.2s) 才满 1.0s
        mod.process([], frame_id=fid, fps=5)
    assert mod.get_stats()["total_exit"] == 1, "退化路径也应按 fps 换算成秒"


# ---------------- A17：空货架按窗口 + 按人去重 + 观察门槛 ----------------

def test_empty_shelf_no_conclusion_before_observe_window():
    """刚启动、观察不足时：不能报"需补货"，要明确标成"数据不足"。"""
    mod = _EmptyShelfModule(FakeROI((SHELF, "shelf_b")))
    mod.process([], frame_id=1, fps=5, timestamp=0.0)
    st = mod.get_stats()
    for z in (SHELF, "shelf_b"):
        assert st["zones"][z]["needs_restock"] is False, st["zones"][z]
        assert st["zones"][z]["insufficient_data"] is True, st["zones"][z]
    assert st["observed_seconds"] < EMPTY_SHELF_MIN_OBSERVE_SECONDS


def test_empty_shelf_flags_restock_after_evidence():
    """观察够久且窗口内确实没人来 → 才报需补货。"""
    mod = _EmptyShelfModule(FakeROI())
    for i in range(5):
        mod.process([], frame_id=i + 1, fps=5,
                    timestamp=i * (EMPTY_SHELF_MIN_OBSERVE_SECONDS / 4.0))
    st = mod.get_stats()
    z = st["zones"][SHELF]
    assert z["insufficient_data"] is False, st
    assert st["observed_seconds"] >= EMPTY_SHELF_MIN_OBSERVE_SECONDS, st
    assert z["needs_restock"] is True, st


def test_empty_shelf_counts_people_not_frames():
    """同一个人在货架前停留 50 帧 = 1 位到访（原实现是 50）。"""
    mod = _EmptyShelfModule(FakeROI())
    for i in range(50):
        mod.process(_tracks(42), frame_id=i + 1, fps=5, timestamp=float(i))
    st = mod.get_stats()
    assert st["zones"][SHELF]["recent_visits"] == 1, st["zones"][SHELF]


def test_empty_shelf_window_prunes_old_visits():
    """窗口外的历史到访不算数：早上来过 3 人，5 分钟后仍应视为"最近没人"。"""
    mod = _EmptyShelfModule(FakeROI())
    mod.process(_tracks(1, 2, 3), frame_id=1, fps=5, timestamp=0.0)
    assert mod.get_stats()["zones"][SHELF]["recent_visits"] == 3
    later = EMPTY_SHELF_WINDOW_SECONDS + 100.0
    mod.process([], frame_id=2, fps=5, timestamp=later)
    st = mod.get_stats()
    assert st["zones"][SHELF]["recent_visits"] == 0, st["zones"][SHELF]
    assert st["zones"][SHELF]["needs_restock"] is True, st


def test_empty_shelf_visits_clear_restock_flag():
    """窗口内有 3 位不同顾客 → 不再报需补货。"""
    mod = _EmptyShelfModule(FakeROI())
    mod.process([], frame_id=1, fps=5, timestamp=0.0)
    mod.process([], frame_id=2, fps=5, timestamp=EMPTY_SHELF_MIN_OBSERVE_SECONDS + 1.0)
    assert mod.get_stats()["zones"][SHELF]["needs_restock"] is True
    mod.process(_tracks(1, 2, 3), frame_id=3, fps=5,
                timestamp=EMPTY_SHELF_MIN_OBSERVE_SECONDS + 2.0)
    st = mod.get_stats()
    assert st["zones"][SHELF]["recent_visits"] == 3, st["zones"][SHELF]
    assert st["zones"][SHELF]["needs_restock"] is False, st["zones"][SHELF]


def test_reset_clears_state():
    """切换视频源时 reset 必须把状态清干净，否则新源会继承旧统计。"""
    mod = _EmptyShelfModule(FakeROI())
    mod.process(_tracks(1, 2), frame_id=1, fps=5, timestamp=0.0)
    mod.reset()
    st = mod.get_stats()
    assert st["zones"][SHELF]["recent_visits"] == 0
    assert st["observed_seconds"] == 0.0
    f = _FootfallModule(FakeROI())
    f.process(_tracks(5), frame_id=1, fps=5, timestamp=0.0)
    f.reset()
    assert f.get_stats()["total_entry"] == 0
