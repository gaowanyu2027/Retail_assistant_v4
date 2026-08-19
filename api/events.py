"""
视频事件触发器 — 规则引擎检测关键事件并主动推送

检测事件：
- crowd_gathering: 某zone内同时人数 >= 阈值
- trajectory_anomaly: 人员离开货架后N秒未进入收银区
- interest_detected: 某人在货架前停留超阈值（SKII-1的实时版本）
"""
from dataclasses import dataclass, field
from typing import Any

from cv_engine.tracker import TrackState
from cv_engine.roi_manager import ROIManager
from config.settings import (
    EVENT_CROWD_THRESHOLD,
    EVENT_CROWD_FRAME_WINDOW,
    EVENT_TRAJECTORY_CHECK_FRAMES,
)


@dataclass
class VideoEvent:
    """视频事件"""
    event_type: str             # "crowd_gathering" | "trajectory_anomaly" | "interest_detected"
    frame_id: int
    timestamp: float
    zone_id: str | None = None
    track_id: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class VideoEventDetector:
    """视频事件检测器

    纯规则引擎，毫秒级响应。不依赖LLM。
    检测到事件后通过WebSocket推送到前端，
    同时可触发MasterAgent做进一步分析。
    """

    def __init__(
        self,
        roi_manager: ROIManager,
        crowd_threshold: int = EVENT_CROWD_THRESHOLD,
        trajectory_check_frames: int = EVENT_TRAJECTORY_CHECK_FRAMES,
    ):
        """
        Args:
            roi_manager: ROI管理器
            crowd_threshold: 人群聚集阈值（同时在场人数）
            trajectory_check_frames: 轨迹异常检查帧数
        """
        self.roi_manager = roi_manager
        self.crowd_threshold = crowd_threshold
        self.trajectory_check_frames = trajectory_check_frames

        # 已触发事件记录（防重复推送）
        self._triggered: set[str] = set()

    def detect(
        self,
        tracks: list[TrackState],
        frame_id: int,
        fps: float,
        timestamp: float,
    ) -> list[VideoEvent]:
        """检测当前帧的所有事件

        Args:
            tracks: 当前帧有效轨迹
            frame_id: 帧号
            fps: 帧率
            timestamp: 时间戳

        Returns:
            检测到的事件列表
        """
        events: list[VideoEvent] = []

        # 1. 人群聚集检测
        events.extend(self._detect_crowd(tracks, frame_id, timestamp))

        # 2. 轨迹异常检测
        events.extend(self._detect_trajectory_anomaly(tracks, frame_id, fps, timestamp))

        return events

    def _detect_crowd(
        self, tracks: list[TrackState], frame_id: int, timestamp: float
    ) -> list[VideoEvent]:
        """检测货架区域人群聚集"""
        events = []
        for zone_id in self.roi_manager.get_shelf_zones():
            count = sum(
                1 for t in tracks
                if self.roi_manager.get_zone(t.center) == zone_id
            )
            if count >= self.crowd_threshold:
                event_key = f"crowd_{zone_id}_{frame_id // EVENT_CROWD_FRAME_WINDOW}"
                if event_key not in self._triggered:
                    self._triggered.add(event_key)
                    events.append(VideoEvent(
                        event_type="crowd_gathering",
                        frame_id=frame_id,
                        timestamp=timestamp,
                        zone_id=zone_id,
                        detail={
                            "person_count": count,
                            "threshold": self.crowd_threshold,
                            "zone_label": self.roi_manager.zone_label.get(zone_id, zone_id),
                        },
                    ))
        return events

    def _detect_trajectory_anomaly(
        self, tracks: list[TrackState], frame_id: int, fps: float, timestamp: float
    ) -> list[VideoEvent]:
        """检测轨迹异常

        条件：track去过货架区 + 未去过收银台 + 现在在出口附近
        """
        events = []
        shelf_zones = set(self.roi_manager.get_shelf_zones())
        checkout_zones = set(self.roi_manager.get_zone_by_type("checkout"))
        exit_zones = set(self.roi_manager.get_zone_by_type("exit"))

        for track in tracks:
            if track.hit_times < self.trajectory_check_frames:
                continue

            visited = set(track.visited_zones.keys())
            has_been_in_shelf = bool(visited & shelf_zones)
            has_been_in_checkout = bool(visited & checkout_zones)
            is_near_exit = bool(visited & exit_zones) or track.is_near_exit

            if has_been_in_shelf and not has_been_in_checkout and is_near_exit:
                event_key = f"traj_anomaly_{track.track_id}"
                if event_key not in self._triggered:
                    self._triggered.add(event_key)
                    events.append(VideoEvent(
                        event_type="trajectory_anomaly",
                        frame_id=frame_id,
                        timestamp=timestamp,
                        track_id=track.track_id,
                        detail={
                            "visited_shelves": list(visited & shelf_zones),
                            "visited_checkout": False,
                            "anomaly_score": track.anomaly_score,
                        },
                    ))

        return events

    def reset(self):
        """重置事件记录"""
        self._triggered.clear()
