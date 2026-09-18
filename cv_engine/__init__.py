# cv_engine 包 — ByteTrack + 表情分析版
"""包入口：**延迟**再导出（PEP 562）。

⚠ 别改回"模块级 re-export"（旧写法曾把 5 个测试文件在 CI 里直接打死）：

```python
from cv_engine.detector import YOLODetector, Detection      # 旧写法
from cv_engine.face_emotion import FaceEmotionDetector
from cv_engine.video_processor import VideoProcessor, FrameResult
```

detector → `ultralytics`（连带 torch），face_emotion → `torch`+`torchvision`+`cv2`，
video_processor → `cv2`。于是**任何** `import cv_engine.*`（哪怕只是要
`roi_manager`/`tracker` 做纯逻辑测试）都会要求装齐深度学习栈 ——
CI 最小依赖 job 里 5 个文件因此导入失败。

延迟后：`from cv_engine import YOLODetector` 照旧可用，但
`from cv_engine.roi_manager import ROIManager` 只需要 yaml + numpy。
"""
from typing import Any

_LAZY = {
    "YOLODetector": ("cv_engine.detector", "YOLODetector"),
    "Detection": ("cv_engine.detector", "Detection"),
    "TrackStateManager": ("cv_engine.tracker", "TrackStateManager"),
    "TrackState": ("cv_engine.tracker", "TrackState"),
    "ROIManager": ("cv_engine.roi_manager", "ROIManager"),
    "FaceEmotionDetector": ("cv_engine.face_emotion", "FaceEmotionDetector"),
    "VideoProcessor": ("cv_engine.video_processor", "VideoProcessor"),
    "FrameResult": ("cv_engine.video_processor", "FrameResult"),
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'cv_engine' has no attribute '{name}'")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
