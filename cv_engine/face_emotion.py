"""
人脸检测 + 表情分类模块
整合 final_work: YOLOv8n-face 检测 + MobileNetV3 7类表情识别
"""
import cv2 as _cv2
import numpy as np
import torch
from torchvision import transforms
from torchvision.models import mobilenet_v3_large
from ultralytics import YOLO

from config.settings import DEVICE, FACE_DETECT_CONF, FACE_MIN_SIZE

# ===== 7类表情 =====
EMOTIONS = ["angry", "disgust", "fear", "happy", "neutral", "sad", "surprise"]
EMOTION_CN = {"angry": "生气", "disgust": "厌恶", "fear": "恐惧",
              "happy": "开心", "neutral": "中性", "sad": "悲伤", "surprise": "惊讶"}
POSITIVE = {"happy", "neutral"}
NEGATIVE = {"angry", "disgust", "fear", "sad"}


def _build_emotion_model(weight_path: str):
    """构建 MobileNetV3-Large 表情分类模型（单通道输入）"""
    model = mobilenet_v3_large(weights=None)
    # 改第一层为单通道
    model.features[0][0] = torch.nn.Conv2d(1, 16, kernel_size=3, stride=2,
                                            padding=1, bias=False)
    # 改分类头为7类
    model.classifier[-1] = torch.nn.Linear(model.classifier[-1].in_features, 7)
    state = torch.load(weight_path, map_location=DEVICE, weights_only=True)
    model.load_state_dict(state)
    model.to(DEVICE).eval()
    return model


class FaceEmotionDetector:
    """人脸检测 + 表情分类联合推理"""

    def __init__(self, face_model_path: str, emotion_weight_path: str):
        """
        Args:
            face_model_path: YOLOv8n-face 权重路径
            emotion_weight_path: MobileNetV3 表情权重路径
        """
        print(f"[FaceEmotion] 加载人脸模型: {face_model_path}")
        self.face_model = YOLO(face_model_path)
        print(f"[FaceEmotion] 加载表情模型: {emotion_weight_path}")
        self.emotion_model = _build_emotion_model(emotion_weight_path)

        self.transform = transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize((48, 48)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5], std=[0.5]),
        ])

    def detect(self, frame: np.ndarray) -> list[dict]:
        """检测并识别一帧中所有人脸的表情

        Args:
            frame: BGR图像 (H, W, 3)

        Returns:
            [{"bbox": [x1,y1,x2,y2], "emotion": str, "conf": float}, ...]
        """
        # 1. YOLO 人脸检测
        results = self.face_model(frame, conf=FACE_DETECT_CONF, device=DEVICE, verbose=False)
        faces = []

        # ultralytics 无检测时 results[0].boxes 为 None，直接迭代会 TypeError
        # （无脸帧在零售/表情视频中是常态，必须判空）
        boxes = results[0].boxes
        if boxes is None:
            return faces

        for box in boxes:
            if box.xyxy is None:
                continue
            x1, y1, x2, y2 = map(int, box.xyxy[0].cpu().numpy())
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(frame.shape[1] - 1, x2), min(frame.shape[0] - 1, y2)
            if x2 - x1 < FACE_MIN_SIZE or y2 - y1 < FACE_MIN_SIZE:
                continue

            # 2. 裁剪人脸 → 灰度 → 表情分类
            face_crop = frame[y1:y2, x1:x2]
            gray = _cv2.cvtColor(face_crop, _cv2.COLOR_BGR2GRAY) if len(face_crop.shape) == 3 else face_crop

            tensor = self.transform(gray).unsqueeze(0).to(DEVICE)
            with torch.no_grad():
                probs = torch.softmax(self.emotion_model(tensor), dim=1)[0]
                idx = int(probs.argmax().item())
                conf = float(probs[idx].item())

            faces.append({
                "bbox": [x1, y1, x2, y2],
                "emotion": EMOTIONS[idx],
                "emotion_cn": EMOTION_CN.get(EMOTIONS[idx], EMOTIONS[idx]),
                "conf": round(conf, 3),
            })

        return faces
