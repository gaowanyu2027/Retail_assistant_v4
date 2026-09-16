"""
轨迹状态管理（轻量） — ByteTrack 已内置在 detector 中完成跟踪
本模块仅维护每条轨迹的元数据：访问区域、异常评分、店员标记等
"""
from typing import Any
from config.settings import TRACK_MAX_LOST

# 退役队列上限。
#
# `_retired` 只是"等上层 drain 后落库"的**暂存**，不是无限缓冲区。
# 但 `cv_engine/multi_stream.py` 的多路常驻引擎只调 process_frame、**从不 drain**，
# 于是它会随客流线性增长。实测（模拟 5000 个行人依次经过）：
#     _tracks(活跃) = 0   _retired(退役) = 5000
#     单条 TrackState 约 296 字节（pickle 估算，实际内存会更高）-> 约 1.4MB
# 单店每天可能新增 1~2MB，一个月几十 MB——是**慢泄漏**，长跑服务会持续占用。
#
# 故保留最近 RETIRED_MAX 条（最可能马上被 drain 的），更早的丢弃并计数。
# 注意：多路场景下的动线本来就没有被持久化（没人 drain），所以丢弃不影响既有功能。
RETIRED_MAX = 1000


class TrackState:
    """单条轨迹的元数据

    ByteTrack 负责 bbox + track_id 的持续关联，
    本类只存业务层关心的状态——区域访问记录、异常评分、店员判定。
    """

    __slots__ = (
        "track_id", "visited_zones", "anomaly_score",
        "is_near_exit", "is_staff", "staff_confidence",
        "last_center", "hit_times", "lost_times",
        "emotion", "emotion_conf",  # 表情标签 + 置信度
        "_last_counted_emotion",  # 已计入统计的表情（防逐帧重复计数）
    )

    def __init__(self, track_id: int, center: tuple[float, float]):
        self.track_id = track_id

        # 区域访问: {zone_id: {"enter_frame": N, "dwell_frames": 0, "counted": False}}
        self.visited_zones: dict[str, dict[str, Any]] = {}

        # 异常 & 店员
        self.anomaly_score: int = 0
        self.is_near_exit: bool = False
        self.is_staff: bool = False          # 是否疑似店员
        self.staff_confidence: float = 0.0   # 店员判定置信度

        # 位置追踪
        self.last_center: tuple[float, float] = center
        self.hit_times: int = 1
        self.lost_times: int = 0

        # 表情（SKII-3）
        self.emotion: str = "unknown"
        self.emotion_conf: float = 0.0
        self._last_counted_emotion: str = "unknown"

    @property
    def center(self) -> tuple[float, float]:
        return self.last_center

    def update_position(self, bbox: list[int]):
        """更新最近位置"""
        x1, y1, x2, y2 = bbox
        self.last_center = ((x1 + x2) / 2, (y1 + y2) / 2)
        self.hit_times += 1
        self.lost_times = 0

    def mark_lost(self):
        """标记一帧未匹配"""
        self.lost_times += 1


class TrackStateManager:
    """轨迹状态管理器

    维护 track_id → TrackState 的映射，自动清理过期轨迹。
    与 ByteTrack 的 persist=True 配合使用。
    """

    def __init__(self, max_lost: int = TRACK_MAX_LOST):
        self._tracks: dict[int, TrackState] = {}
        self.max_lost = max_lost
        # 退役队列：cleanup 删除的轨迹暂存于此，供上层（持久化层）drain 后落库。
        # CV 层保持零 DB 依赖，由调用方决定如何处理（如写入动线分析表）。
        self._retired: list[TrackState] = []
        # 因超过 RETIRED_MAX 被丢弃的条数（可观测性：能看出"没人 drain"）
        self.retired_dropped: int = 0

    def drain_retired(self) -> list[TrackState]:
        """取走并清空退役轨迹队列。"""
        out = self._retired
        self._retired = []
        return out

    def get_or_create(self, track_id: int, bbox: list[int]) -> TrackState:
        """获取或创建轨迹状态"""
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        if track_id not in self._tracks:
            self._tracks[track_id] = TrackState(track_id, (cx, cy))
        else:
            self._tracks[track_id].update_position(bbox)
        return self._tracks[track_id]

    def get(self, track_id: int) -> TrackState | None:
        return self._tracks.get(track_id)

    def mark_all_lost(self):
        """标记所有轨迹一帧未匹配"""
        for t in self._tracks.values():
            t.mark_lost()

    def cleanup(self):
        """清理长期未匹配的轨迹（被清理的轨迹进入退役队列，供上层持久化动线）"""
        stale = [
            tid for tid, t in self._tracks.items()
            if t.lost_times > self.max_lost
        ]
        for tid in stale:
            self._retired.append(self._tracks[tid])
            del self._tracks[tid]

        # 有界化：见 RETIRED_MAX 的说明。没人 drain 时（多路常驻引擎）
        # 否则这里会随客流线性增长。
        overflow = len(self._retired) - RETIRED_MAX
        if overflow > 0:
            del self._retired[:overflow]
            self.retired_dropped += overflow
            # 只在首次丢弃时提示一次，避免刷屏
            if self.retired_dropped == overflow:
                print(f"[Tracker] 退役轨迹队列超过 {RETIRED_MAX} 条，"
                      f"开始丢弃最旧的（说明调用方没有 drain，动线不会被持久化）")

    def get_active(self) -> list[TrackState]:
        """获取活跃轨迹（最近有匹配）"""
        self.cleanup()
        return [t for t in self._tracks.values() if t.lost_times == 0]

    def get_all(self) -> list[TrackState]:
        return list(self._tracks.values())

    @property
    def active_count(self) -> int:
        return len(self.get_active())

    def reset(self):
        self._tracks.clear()
        self._retired.clear()
