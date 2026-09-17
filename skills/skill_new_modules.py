"""
新分析模块 — 客流计数 / 空货架提醒（阶段二，填进候选池）

模块通过 register_module 注册，AnalyticsModule 子类支持 process(喂轨迹) + get_stats(统计)。
基于轨迹的"虚拟线穿越计数"实现客流/空货架，不与现有 skill 冲突。

- footfall:       门口客流计数（虚拟线穿越 + 分时段聚合）
- empty_shelf:    货架空置检测（轨迹稀少 + 区域访问量低 → 补货提醒）
"""
from collections import defaultdict

from agents.analytics_module import AnalyticsModule, register_module
from config.settings import (
    EMPTY_SHELF_MIN_OBSERVE_SECONDS,
    EMPTY_SHELF_MIN_VISITS,
    EMPTY_SHELF_WINDOW_SECONDS,
    FOOTFALL_EXIT_MISSING_SECONDS,
    VIDEO_FPS,
)


def _now_seconds(timestamp, frame_id, fps) -> float:
    """返回当前时刻（秒）。

    **优先用管线给的真实经过时间**（`video_processor` 里是 `time.monotonic() - t0`），
    只有在它缺失时才退化用 `帧号 / fps` 换算。原因：文件源并不按实时播放
    （解码跑得比 30fps 快得多），用声明 fps 换算会把秒数系统性算短。
    """
    if timestamp is not None:
        try:
            return float(timestamp)
        except (TypeError, ValueError):
            pass
    try:
        f = float(fps)
    except (TypeError, ValueError):
        f = 0.0
    if f <= 0:
        f = float(VIDEO_FPS) if VIDEO_FPS else 30.0
    return float(frame_id or 0) / f


class _FootfallModule(AnalyticsModule):
    """门口客流计数：门口摄像头画面中出现的轨迹计为进店（去重），轨迹丢失计外出。

    ⚠ 离开判定按**秒**（`FOOTFALL_EXIT_MISSING_SECONDS`），不是按帧：
    原实现写死 30 帧 —— 30fps 下恰好 1 秒，但 fps=5 时等价 6 秒，
    于是"在店时长/外出时刻"整体错 6 倍（台账 A18）。
    """

    name = "footfall"
    type_hint = ["entrance"]

    def __init__(self, roi_manager):
        self._roi = roi_manager
        self._counted: dict[int, int] = {}          # track_id → 状态(1在店/-1已离)
        self._last_seen: dict[int, float] = {}      # track_id → 最后一次出现的时刻(秒)
        self._entry_count = 0
        self._exit_count = 0
        self._hourly: dict[str, dict] = defaultdict(lambda: {"in": 0, "out": 0})

    def process(self, tracks, frame_id, fps, timestamp=None, current_hour=None):
        now = _now_seconds(timestamp, frame_id, fps)
        seen: set[int] = set()
        for t in tracks:
            tid = t.track_id
            # 轨迹首次出现（且不是误检）→ 计一次进店（去重）
            if tid not in self._counted and getattr(t, "hit_times", 0) > 2:
                self._counted[tid] = 1
                self._entry_count += 1
                if current_hour:
                    self._hourly[current_hour]["in"] += 1
            if self._counted.get(tid) == 1:
                seen.add(tid)
                self._last_seen[tid] = now       # 出现即刷新，"重新出现"自然取消离开判定
        # 离开判定：进店轨迹**连续未出现达到阈值秒数** → 计外出（去重）
        # 不再依赖 lost_times（那是轨迹丢失标志，活跃轨迹恒为 0，导致外出永不计）
        for tid, state in list(self._counted.items()):
            if state != 1 or tid in seen:
                continue
            last = self._last_seen.get(tid, now)
            if now - last >= FOOTFALL_EXIT_MISSING_SECONDS:
                self._counted[tid] = -1
                self._exit_count += 1
                if current_hour:
                    self._hourly[current_hour]["out"] += 1
                self._last_seen.pop(tid, None)

    def get_stats(self, **kw) -> dict:
        return {
            "total_entry": self._entry_count,
            "total_exit": self._exit_count,
            "net_visit": max(self._entry_count - self._exit_count, 0),
            "hourly": dict(self._hourly),
            # 把口径一起暴露出去：报表解读时能看出"多久没出现算离店"
            "exit_missing_seconds": FOOTFALL_EXIT_MISSING_SECONDS,
        }

    def reset(self):
        self._counted.clear(); self._last_seen.clear()
        self._entry_count = 0; self._exit_count = 0; self._hourly.clear()


class _EmptyShelfModule(AnalyticsModule):
    """空货架提醒：**最近窗口内**到访人数持续低 + 观察时间够长 → 判断可能缺货/补货。

    ⚠ 原实现两个口径错误（台账 A17）：
    1. `_zone_visits` 是**自模块创建以来**的累计值，没有时间窗 —— 早上来过的货架，
       下午一直没人也仍然"到访数达标"；
    2. 它按**帧**累加：一个人站在货架前 30 秒会被算成 30 次到访。
    现在改为：窗口内按 `track_id` **去重计数**，且观察时长不足时明确"数据不足"而不是报缺货。
    """

    name = "empty_shelf"
    type_hint = ["indoor_shelf"]

    def __init__(self, roi_manager):
        self._roi = roi_manager
        self._zone_seen: dict[str, dict[int, float]] = defaultdict(dict)  # zone → {track_id: 最后出现时刻}
        self._t0: float | None = None
        self._last_now: float = 0.0

    def process(self, tracks, frame_id, fps, timestamp=None, current_hour=None):
        now = _now_seconds(timestamp, frame_id, fps)
        if self._t0 is None:
            self._t0 = now
        self._last_now = now
        shelf_zones = set(self._roi.get_shelf_zones())
        for t in tracks:
            zid = self._roi.get_zone(getattr(t, "center", (0, 0)))
            if zid and zid in shelf_zones and getattr(t, "hit_times", 0) > 2:
                self._zone_seen[zid][getattr(t, "track_id", -1)] = now
        # 只保留窗口内的到访（按人、按最后出现时刻老化）
        cutoff = now - EMPTY_SHELF_WINDOW_SECONDS
        for zid, seen in self._zone_seen.items():
            for tid in [k for k, ts in seen.items() if ts < cutoff]:
                seen.pop(tid, None)

    def _observed_seconds(self) -> float:
        if self._t0 is None:
            return 0.0
        return max(0.0, self._last_now - self._t0)

    def get_stats(self, **kw) -> dict:
        observed = self._observed_seconds()
        enough = observed >= EMPTY_SHELF_MIN_OBSERVE_SECONDS
        out = {}
        for zid in self._roi.get_shelf_zones():
            v = len(self._zone_seen.get(zid, {}))
            out[zid] = {
                "recent_visits": v,                       # 窗口内**去重后**的到访人数
                "needs_restock": bool(enough and v < EMPTY_SHELF_MIN_VISITS),
                # 观察不足时明确标出来：此时"到访 0"是"还不知道"，不是"没货"
                "insufficient_data": not enough,
                "label": self._roi.zone_label.get(zid, zid),
            }
        return {
            "zones": out,
            "window_seconds": EMPTY_SHELF_WINDOW_SECONDS,
            "observed_seconds": round(observed, 1),
            "min_observe_seconds": EMPTY_SHELF_MIN_OBSERVE_SECONDS,
            "min_visits": EMPTY_SHELF_MIN_VISITS,
        }

    def reset(self):
        self._zone_seen.clear()
        self._t0 = None
        self._last_now = 0.0


def _register_new_modules():
    register_module("footfall", ["entrance"], lambda r: _FootfallModule(r))
    register_module("empty_shelf", ["indoor_shelf"], lambda r: _EmptyShelfModule(r))


_register_new_modules()
