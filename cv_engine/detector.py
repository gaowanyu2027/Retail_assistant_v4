"""
YOLO检测器封装 — YOLO26l + ByteTrack 内置跟踪
"""
from dataclasses import dataclass
import numpy as np
from ultralytics import YOLO
from config.settings import (
    YOLO_MODEL_PATH, DEVICE, YOLO_CONF, YOLO_IOU,
    YOLO_IMGSZ, PERSON_CLASS_ID, USE_FP16, YOLO_TRACKER
)


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
