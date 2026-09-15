"""
视频处理器 — YOLO+ByteTrack → 轨迹状态 → 标注
"""
import os
import cv2
import numpy as np
from dataclasses import dataclass, field
from typing import Generator

from cv_engine.detector import YOLODetector, Detection
from cv_engine.tracker import TrackStateManager, TrackState
from cv_engine.roi_manager import ROIManager
from config.settings import VIDEO_FPS, FRAME_SKIP, FACE_EMOTION_INTERVAL


@dataclass
class FrameResult:
    """单帧处理结果"""
    frame_id: int
    frame: np.ndarray
    annotated_frame: np.ndarray
    tracks: list[TrackState]
    detections: list[Detection]
    timestamp: float


class VideoProcessor:
    """视频处理流水线（ByteTrack 版）

    YOLO+ByteTrack检测跟踪 → TrackState 元数据维护 → ROI判定 → 标注。
    """

    def __init__(
        self,
        detector: YOLODetector,
        track_manager: TrackStateManager,
        roi_manager: ROIManager,
        fps: float = VIDEO_FPS,
        frame_skip: int = FRAME_SKIP,
        face_emotion: "FaceEmotionDetector | None" = None,  # type: ignore
        face_emotion_interval: int = FACE_EMOTION_INTERVAL,
    ):
        self.detector = detector
        self.track_mgr = track_manager
        self.roi_manager = roi_manager
        self.fps = fps
        self.frame_skip = max(1, frame_skip)
        self.face_emotion = face_emotion
        self.face_emotion_interval = max(1, face_emotion_interval)
        self._frame_id = 0
        # 真实时间基准：进程启动/重置时的单调时钟，timestamp 用它计算，
        # 不受自适应降帧/丢帧影响（此前用 frame_id/fps 标称折算会严重偏离）
        import time
        self._t0 = time.monotonic()

    def reset(self):
        self._frame_id = 0
        import time
        self._t0 = time.monotonic()
        self.track_mgr.reset()

    def process_frame(self, frame: np.ndarray) -> FrameResult:
        """处理单帧"""
        import time
        self._frame_id += 1
        timestamp = time.monotonic() - self._t0

        h, w = frame.shape[:2]
        self.roi_manager.set_frame_size(w, h)
        detections: list[Detection] = []
        tracks: list[TrackState] = []

        # 帧跳过：跳过的帧复用上一帧检测结果
        skip_detection = (
            self._frame_id % self.frame_skip != 0
            and self.track_mgr.active_count > 0
        )

        if skip_detection:
            # 跳帧：不做检测，直接复用上一帧的活跃轨迹
            tracks = self.track_mgr.get_active()
        else:
            detections = self.detector.detect_and_track(frame)
            # 标记未匹配轨迹
            matched_ids = {d.track_id for d in detections}
            for t in self.track_mgr.get_all():
                if t.track_id not in matched_ids:
                    t.mark_lost()

            # 更新轨迹状态
            tracks: list[TrackState] = []
            for det in detections:
                state = self.track_mgr.get_or_create(det.track_id, det.bbox)

                # ROI 判定
                zone_id = self.roi_manager.get_zone(state.center)
                if zone_id is not None:
                    if zone_id not in state.visited_zones:
                        state.visited_zones[zone_id] = {
                            "enter_frame": self._frame_id,
                            "dwell_frames": 0,
                            "counted": False,
                            "exit_frame": None,
                        }
                    # 注意：dwell_frames 统一由 skills.skill_popularity.process 累加，
                    # 这里不再递增，避免同一帧被两处累加导致停留时长翻倍
                else:
                    for zid, record in state.visited_zones.items():
                        if record.get("exit_frame") is None:
                            record["exit_frame"] = self._frame_id

                state.is_near_exit = any(
                    self.roi_manager.is_exit_zone(z)
                    for z in state.visited_zones
                )
                tracks.append(state)

            # ---- 人脸表情检测（SKII-3），仅在检测帧运行 ----
            if (
                self.face_emotion
                and tracks
                and self._frame_id % (self.frame_skip * self.face_emotion_interval) == 0
            ):
                faces = self.face_emotion.detect(frame)
                # 表情关联到最近的行人（根据 bbox 重叠）
                faces = faces or []
                for face in faces:
                    fx1, fy1, fx2, fy2 = face["bbox"]
                    matched = False
                    for t in tracks:
                        for det in detections:
                            if det.track_id == t.track_id:
                                bx1, by1, bx2, by2 = det.bbox
                                # 人脸框完全在行人 bbox 内则关联
                                if fx1 >= bx1 and fy1 >= by1 and fx2 <= bx2 and fy2 <= by2:
                                    t.emotion = face["emotion"]
                                    t.emotion_conf = face["conf"]
                                    matched = True
                                    break
                        if matched:
                            break

        # 标注
        annotated = self._annotate_frame(frame.copy(), tracks, detections)

        return FrameResult(
            frame_id=self._frame_id,
            frame=frame,
            annotated_frame=annotated,
            tracks=tracks,
            detections=detections,
            timestamp=timestamp,
        )

    def _annotate_frame(
        self, frame: np.ndarray, tracks: list[TrackState], detections: list[Detection]
    ) -> np.ndarray:
        frame = self.roi_manager.draw_zones(frame)

        det_map = {d.track_id: d for d in detections}
        for t in tracks:
            det = det_map.get(t.track_id)
            if det is None:
                continue
            x1, y1, x2, y2 = det.bbox
            if t.anomaly_score >= 50:
                color = (0, 0, 255)
            elif t.is_staff:
                color = (255, 200, 0)  # 金色 = 店员
            else:
                color = (255, 0, 0)

            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            label = f"ID:{t.track_id}"
            if t.is_staff:
                label += " [店员]"
            elif t.anomaly_score >= 50:
                label += f" [!{t.anomaly_score}]"
            cv2.putText(frame, label, (x1, y1 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        cv2.putText(frame, f"Frame:{self._frame_id} Tracks:{len(tracks)}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        return frame

    # ============ 输入源（不变） ============

    def run_image_sequence(self, img_dir: str) -> Generator[FrameResult, None, None]:
        img_paths = sorted([
            os.path.join(img_dir, f) for f in os.listdir(img_dir)
            if f.endswith((".jpg", ".png", ".jpeg"))
        ])
        for path in img_paths:
            frame = cv2.imread(path)
            if frame is None:
                continue
            yield self.process_frame(frame)

    def run_video_file(self, video_path: str) -> Generator[FrameResult, None, None]:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            yield self.process_frame(frame)
        cap.release()

    def run_webcam(self, camera_id: int = 0) -> Generator[FrameResult, None, None]:
        cap = cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            yield self.process_frame(frame)
        cap.release()
