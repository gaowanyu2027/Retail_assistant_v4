"""
新分析模块 — 客流计数 / 空货架提醒（阶段二，填进候选池）

模块通过 register_module 注册，AnalyticsModule 子类支持 process(喂轨迹) + get_stats(统计)。
基于轨迹的"虚拟线穿越计数"实现客流/空货架，不与现有 skill 冲突。

- footfall:       门口客流计数（虚拟线穿越 + 分时段聚合）
- empty_shelf:    货架空置检测（轨迹稀少 + 区域访问量低 → 补货提醒）
"""
from collections import defaultdict
from agents.analytics_module import AnalyticsModule, register_module


class _FootfallModule(AnalyticsModule):
    """门口客流计数：门口摄像头画面中出现的轨迹计为进店（去重），轨迹丢失计外出。"""

    name = "footfall"
    type_hint = ["entrance"]

    def __init__(self, roi_manager):
        self._roi = roi_manager
        self._counted: dict[int, int] = {}          # track_id → 状态(1在店/-1已离)
        self._missing_frames: dict[int, int] = {}   # 进店轨迹连续未出现帧数（缓冲）
        self._entry_count = 0
        self._exit_count = 0
        self._hourly: dict[str, dict] = defaultdict(lambda: {"in": 0, "out": 0})

    def process(self, tracks, frame_id, fps, timestamp=None, current_hour=None):
        current_ids = set()
        for t in tracks:
            tid = t.track_id
            # 轨迹首次出现（且不是误检）→ 计一次进店（去重）
            if tid not in self._counted and getattr(t, "hit_times", 0) > 2:
                self._counted[tid] = 1
                self._entry_count += 1
                if current_hour:
                    self._hourly[current_hour]["in"] += 1
            if tid in self._counted and self._counted[tid] == 1:
                current_ids.add(tid)
                self._missing_frames[tid] = 0
        # 离开判定：进店轨迹本帧未出现 → 连续 MISSING_LIMIT 帧未现 → 计外出（去重）
        # 不再依赖 lost_times（那是轨迹丢失标志，活跃轨迹恒为 0，导致外出永不计）
        missing_limit = 30
        for tid in list(self._counted):
            if self._counted[tid] == 1 and tid not in current_ids:
                self._missing_frames[tid] = self._missing_frames.get(tid, 0) + 1
                if self._missing_frames[tid] >= missing_limit:
                    self._counted[tid] = -1
                    self._exit_count += 1
                    if current_hour:
                        self._hourly[current_hour]["out"] += 1
            elif tid in self._missing_frames:
                self._missing_frames.pop(tid, None)

    def get_stats(self, **kw) -> dict:
        return {
            "total_entry": self._entry_count,
            "total_exit": self._exit_count,
            "net_visit": max(self._entry_count - self._exit_count, 0),
            "hourly": dict(self._hourly),
        }

    def reset(self):
        self._counted.clear(); self._entry_count = 0; self._exit_count = 0; self._hourly.clear()


class _EmptyShelfModule(AnalyticsModule):
    """空货架提醒：区域访问量持续低 + 无轨迹停留 → 判断可能缺货/补货。"""

    name = "empty_shelf"
    type_hint = ["indoor_shelf"]

    def __init__(self, roi_manager):
        self._roi = roi_manager
        self._zone_visits: dict[str, int] = defaultdict(int)   # zone_id → 近期到访
        self._low_activity = False

    def process(self, tracks, frame_id, fps, timestamp=None, current_hour=None):
        shelf_zones = set(self._roi.get_shelf_zones())
        for t in tracks:
            zid = self._roi.get_zone(getattr(t, "center", (0, 0)))
            if zid and zid in shelf_zones and getattr(t, "hit_times", 0) > 2:
                self._zone_visits[zid] += 1

    def get_stats(self, **kw) -> dict:
        # 简化判定：到访数极低的货架 → 标记"可能空置/需补货"
        zones = self._roi.get_shelf_zones()
        out = {}
        for zid in zones:
            v = self._zone_visits.get(zid, 0)
            out[zid] = {
                "recent_visits": v,
                "needs_restock": v < 3,      # 近段时间到访 < 3 视为可能空置
                "label": self._roi.zone_label.get(zid, zid),
            }
        return {"zones": out}

    def reset(self):
        self._zone_visits.clear(); self._low_activity = False


def _register_new_modules():
    register_module("footfall", ["entrance"], lambda r: _FootfallModule(r))
    register_module("empty_shelf", ["indoor_shelf"], lambda r: _EmptyShelfModule(r))


_register_new_modules()
