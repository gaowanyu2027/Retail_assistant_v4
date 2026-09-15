"""
多路视频采集引擎 — 后台常驻并行分析，每路喂独立模块（数据隔离）

与 WebSocket 单路驱动不同，本引擎按 cameras.yaml 为每个摄像头启动采集线程：
  VideoCapture(source) → VideoProcessor(独立 tracker/roi) → 检测/跟踪
  → 喂 module_registry 对应摄像头的 process_frame（数据隔离）

多路真并行：每摄像头独立 detector 实例（per-camera YOLO），去掉共享锁串行化——
各路同时推理，吞吐随路数提升（代价：每路一份模型显存/内存，可按需限制并行路数）。

多路视频源（webcam/rtsp/文件/图片序列）由 cameras.yaml 的 source 指定。
"""
import threading
import time
from pathlib import Path

import cv2

from config.settings import (
    PROJECT_ROOT, FRAME_SKIP, VIDEO_FPS,
    YOLO_MODEL_PATH, DEVICE, YOLO_CONF, YOLO_IOU, YOLO_IMGSZ,
)
from agents.module_registry import get_registry
from cv_engine.detector import YOLODetector
from cv_engine.tracker import TrackStateManager
from cv_engine.video_processor import VideoProcessor


class _StreamThread(threading.Thread):
    """单个摄像头的采集分析线程（独立 detector，并行推理）。"""

    def __init__(self, cam_id, source, fps, module_registry, detector):
        super().__init__(daemon=True)
        self.cam_id = cam_id
        self.source = source
        self.fps = fps
        self.registry = module_registry
        self.detector = detector
        self._stop = threading.Event()

    def _open(self):
        """打开视频源：webcam / rtsp:// / 文件 / 图片序列目录。"""
        s = str(self.source)
        if s.startswith("rtsp://"):
            cap = cv2.VideoCapture(s)
        elif s.isdigit() or s == "webcam":
            idx = 0 if s == "webcam" else int(s)
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        else:
            p = Path(s)
            if not p.is_absolute():
                p = PROJECT_ROOT / s
            cap = cv2.VideoCapture(str(p))
        return cap

    def run(self):
        cap = self._open()
        if not cap.isOpened():
            print(f"[MultiStream] {self.cam_id} 打开视频源失败: {self.source}")
            return
        # 独立 tracker + ROI（每路一份，数据隔离）
        from api.dependencies import get_roi_manager
        try:
            roi_mgr = get_roi_manager()
        except Exception:
            roi_mgr = None
        track_mgr = TrackStateManager()
        processor = VideoProcessor(self.detector, track_mgr, roi_mgr, frame_skip=FRAME_SKIP, fps=self.fps)
        # 绑定 module_registry 的该摄像头（喂数据）
        cam = self.registry.get_camera(self.cam_id)

        print(f"[MultiStream] {self.cam_id} 分析线程启动 [{self.source}]")
        t0 = time.time()
        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            # 每路独立 detector → 无需共享锁，可并行推理（多路真并行）
            result = processor.process_frame(frame)
            # 喂给该摄像头模块（数据隔离：只喂自己）
            if cam is not None:
                try:
                    now = time.time()
                    cam.process_frame(result.tracks, result.frame_id, self.fps,
                                      timestamp=now - t0,
                                      current_hour=time.strftime("%Y%m%d%H"))
                except Exception as e:
                    print(f"[MultiStream] {self.cam_id} 喂模块失败: {e}")
        cap.release()
        print(f"[MultiStream] {self.cam_id} 分析线程停止")

    def stop(self):
        self._stop.set()


class MultiStreamEngine:
    """管理多路摄像头的采集分析线程（每路独立 detector 并行）。"""

    def __init__(self):
        self._threads: dict[str, _StreamThread] = {}
        self._detectors: dict[str, YOLODetector] = {}
        self._lock = threading.Lock()

    def _get_detector(self, cam_id: str) -> YOLODetector:
        """每摄像头独立 detector 实例（per-camera，并行推理）。"""
        if cam_id not in self._detectors:
            model_path = str(PROJECT_ROOT / YOLO_MODEL_PATH)
            self._detectors[cam_id] = YOLODetector(
                model_path=model_path, device=DEVICE,
                conf=YOLO_CONF, iou=YOLO_IOU, imgsz=YOLO_IMGSZ,
            )
        return self._detectors[cam_id]

    def start(self, cam_id: str | None = None) -> dict:
        """启动一路（默认启动所有已配 source 的摄像头）。"""
        reg = get_registry()
        started = []
        for c in reg.list_cameras():
            cid = c["id"]
            if cam_id and cid != cam_id:
                continue
            if cid in self._threads and self._threads[cid].is_alive():
                continue
            source = c.get("source")
            if not source:
                continue
            t = _StreamThread(cid, source, VIDEO_FPS, reg, self._get_detector(cid))
            with self._lock:
                self._threads[cid] = t
            t.start()
            started.append(cid)
        return {"ok": True, "started": started}

    def stop(self, cam_id: str | None = None) -> dict:
        stopped = []
        with self._lock:
            for cid, t in list(self._threads.items()):
                if cam_id and cid != cam_id:
                    continue
                t.stop()
                stopped.append(cid)
                self._threads.pop(cid, None)
        return {"ok": True, "stopped": stopped}

    def status(self) -> list[dict]:
        out = []
        for cid, t in self._threads.items():
            out.append({"camera": cid, "running": t.is_alive(), "source": t.source})
        return out


_engine: MultiStreamEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> MultiStreamEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = MultiStreamEngine()
    return _engine
