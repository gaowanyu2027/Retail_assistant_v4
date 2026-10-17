"""
YOLO检测器封装 — YOLO26n + ByteTrack 内置跟踪
"""
import os
from dataclasses import dataclass
import numpy as np
import torch

from config.settings import (
    YOLO_MODEL_PATH, DEVICE, YOLO_CONF, YOLO_IOU,
    YOLO_IMGSZ, PERSON_CLASS_ID, USE_FP16, YOLO_TRACKER,
    YOLO_NUM_THREADS,
)

# Ultralytics 的两处默认值会让 2 vCPU 机器上的 CPU 推理只跑 1 个线程：
#   1) ultralytics/__init__.py 在 OMP_NUM_THREADS 未设置时强制设为 1；
#   2) select_device('cpu') 调用 torch.set_num_threads(NUM_THREADS)，而
#      NUM_THREADS = min(8, max(1, os.cpu_count() - 1)) —— 2 核时正好是 1。
# YOLO_NUM_THREADS > 0 时，在导入 ultralytics 前设好 OMP，再覆盖其
# torch_utils.NUM_THREADS，让后续每次 select_device 都使用配置值。
# 默认 0 = 不改动，保持原行为。
if YOLO_NUM_THREADS > 0:
    os.environ["OMP_NUM_THREADS"] = str(YOLO_NUM_THREADS)
    os.environ["MKL_NUM_THREADS"] = str(YOLO_NUM_THREADS)

from ultralytics import YOLO  # noqa: E402  (必须先设置 OMP_NUM_THREADS)
from ultralytics.utils import torch_utils as _yolo_torch_utils  # noqa: E402

if YOLO_NUM_THREADS > 0:
    _yolo_torch_utils.NUM_THREADS = YOLO_NUM_THREADS
    torch.set_num_threads(YOLO_NUM_THREADS)


@dataclass
class Detection:
    """单条检测/跟踪结果"""
    bbox: list[int]        # [x1, y1, x2, y2]
    track_id: int          # ByteTrack 分配的跟踪ID（-1=未关联）
    cls: int = 0           # 类别ID
    conf: float = 0.0      # 置信度
    center: tuple[float, float] = (0, 0)


class YOLODetector:
    """YOLO + ByteTrack 检测器

    内置 ByteTrack 跟踪，取代独立的 DeepSORT 模块：
    - 速度更快（无 ReID 特征提取）
    - 代码更少（删 kalman/reid/级联匹配）
    - 显存更省（只跑 YOLO 一个模型）
    """

    def __init__(
        self,
        model_path: str = YOLO_MODEL_PATH,
        device: str = DEVICE,
        conf: float = YOLO_CONF,
        iou: float = YOLO_IOU,
        imgsz: int = YOLO_IMGSZ,
        classes: list[int] | None = None,
    ):
        self.model_path = model_path
        self.device = device
        self.conf = conf
        self.iou = iou
        self.imgsz = imgsz
        self.classes = classes if classes is not None else [PERSON_CLASS_ID]

        print(f"[YOLODetector] 加载模型: {model_path} (ByteTrack 内置)")
        self.model = YOLO(model_path)
        print(f"[YOLODetector] 设备: {device}")

    def detect_and_track(self, frame: np.ndarray) -> list[Detection]:
        """检测行人 + ByteTrack 跟踪（一次推理完成）

        Args:
            frame: BGR格式图像 (H, W, 3)

        Returns:
            Detection 列表，含 bbox + track_id
        """
        results = self.model.track(
            frame,
            conf=self.conf,
            iou=self.iou,
            imgsz=self.imgsz,
            classes=self.classes,
            device=self.device,
            tracker=YOLO_TRACKER,
            persist=True,
            verbose=False,
        )

        detections = []
        boxes = results[0].boxes
        if boxes is None or boxes.id is None:
            return detections

        for i in range(len(boxes)):
            xyxy = boxes.xyxy[i].cpu().numpy()
            track_id = int(boxes.id[i].cpu().item())
            conf_val = float(boxes.conf[i].cpu().item()) if boxes.conf is not None else 1.0
            cls_val = int(boxes.cls[i].cpu().item()) if boxes.cls is not None else 0

            x1, y1, x2, y2 = map(int, xyxy)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2

            detections.append(Detection(
                bbox=[x1, y1, x2, y2],
                track_id=track_id,
                cls=cls_val,
                conf=conf_val,
                center=(cx, cy),
            ))

        return detections

    def detect_all(self, frame: np.ndarray) -> list[dict]:
        """检测所有类别（商品、购物车等扩展用途）

        Returns:
            [{"bbox": [x1,y1,x2,y2], "cls": int, "conf": float}, ...]
        """
        results = self.model(
            frame,
            conf=self.conf,
            iou=self.iou,
            imgsz=self.imgsz,
            classes=None,
            device=self.device,
            verbose=False,
        )
        detections = []
        for pred in results[0].boxes:
            if pred.xyxy is not None:
                detections.append({
                    "bbox": pred.xyxy.cpu().numpy()[0],
                    "cls": int(pred.cls.cpu().numpy()[0]),
                    "conf": float(pred.conf.cpu().numpy()[0]),
                })
        return detections
