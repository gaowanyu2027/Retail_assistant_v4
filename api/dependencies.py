"""
FastAPI 依赖注入模块 — 管理全局单例实例
整合零售分析 + 人脸表情分析双系统
"""
import os
import time
import yaml
from functools import lru_cache

from config.settings import (
    PROJECT_ROOT,
    YOLO_MODEL_PATH, FACE_MODEL_PATH, EMOTION_MODEL_PATH,
    DEVICE, ROI_CONFIG_PATH, THRESHOLDS_CONFIG_PATH,
    YOLO_CONF, YOLO_IOU, YOLO_IMGSZ,
)
from cv_engine.detector import YOLODetector
from cv_engine.tracker import TrackStateManager
from cv_engine.roi_manager import ROIManager

# ==================== 全局单例（懒加载） ====================

_detector: YOLODetector | None = None
_tracker: TrackStateManager | None = None
_roi_manager: ROIManager | None = None
_thresholds_config: dict | None = None
_pop_skill: "PopularitySkill | None" = None
_anom_skill: "AnomalySkill | None" = None
_emo_skill: "SkillEmotion | None" = None
_face_emotion: "FaceEmotionDetector | None" = None
_start_time: float = time.time()


def get_detector() -> YOLODetector:
    """获取YOLO检测器单例"""
    global _detector
    if _detector is None:
        model_path = str(PROJECT_ROOT / YOLO_MODEL_PATH)
        if not os.path.exists(model_path):
            print(f"[Dependencies] YOLO模型权重不存在，尝试自动下载: {model_path}")
        print(f"[Dependencies] 加载YOLO模型: {model_path}")
        _detector = YOLODetector(
            model_path=model_path,
            device=DEVICE,
            conf=YOLO_CONF,
            iou=YOLO_IOU,
            imgsz=YOLO_IMGSZ,
        )
    return _detector


def get_tracker() -> TrackStateManager:
    """获取轨迹状态管理器单例"""
    global _tracker
    if _tracker is None:
        print("[Dependencies] 初始化 TrackStateManager")
        _tracker = TrackStateManager()
    return _tracker


def get_roi_manager() -> ROIManager:
    """获取ROI管理器单例"""
    global _roi_manager
    if _roi_manager is None:
        roi_path = str(PROJECT_ROOT / ROI_CONFIG_PATH)
        if not os.path.exists(roi_path):
            raise FileNotFoundError(f"ROI配置文件不存在: {roi_path}")
        print(f"[Dependencies] 加载ROI配置: {roi_path}")
        _roi_manager = ROIManager(roi_path)
    return _roi_manager


def get_thresholds_config() -> dict:
    """获取阈值配置"""
    global _thresholds_config
    if _thresholds_config is None:
        thresh_path = str(PROJECT_ROOT / THRESHOLDS_CONFIG_PATH)
        if not os.path.exists(thresh_path):
            raise FileNotFoundError(f"阈值配置文件不存在: {thresh_path}")
        with open(thresh_path, "r", encoding="utf-8") as f:
            _thresholds_config = yaml.safe_load(f)
    return _thresholds_config


def get_uptime_seconds() -> float:
    """获取服务运行时长（秒）"""
    return time.time() - _start_time


def get_popularity_skill():
    """获取货架热度技能单例"""
    global _pop_skill
    if _pop_skill is None:
        from skills.skill_popularity import PopularitySkill
        thresholds = get_thresholds_config()
        pop_cfg = thresholds.get("popularity", {})
        _pop_skill = PopularitySkill(
            roi_manager=get_roi_manager(),
            dwell_threshold=pop_cfg.get("dwell_threshold_seconds", 30),
            staff_threshold=pop_cfg.get("staff_threshold_seconds", 900),
            min_track_hits=pop_cfg.get("min_track_hits", 3),
        )
    return _pop_skill


def get_anomaly_skill():
    """获取异常检测技能单例"""
    global _anom_skill
    if _anom_skill is None:
        from skills.skill_anomaly import AnomalySkill
        thresholds = get_thresholds_config()
        anom_cfg = thresholds.get("anomaly", {})
        _anom_skill = AnomalySkill(
            roi_manager=get_roi_manager(),
            alert_threshold_watch=anom_cfg.get("alert_threshold_watch", 50),
            alert_threshold_high=anom_cfg.get("alert_threshold_high", 70),
            l1_weights=anom_cfg.get("l1_weights") or None,
            l2_weights=anom_cfg.get("l2_weights") or None,
            l3_weights=anom_cfg.get("l3_weights") or None,
        )
    return _anom_skill


def get_emotion_skill():
    """获取表情分析技能单例（零售模式内存版）"""
    global _emo_skill
    if _emo_skill is None:
        from skills.skill_emotion import SkillEmotion
        _emo_skill = SkillEmotion()
    return _emo_skill


def get_face_emotion():
    """获取人脸表情检测器单例"""
    global _face_emotion
    if _face_emotion is None:
        from cv_engine.face_emotion import FaceEmotionDetector
        face_path = str(PROJECT_ROOT / FACE_MODEL_PATH)
        emo_path = str(PROJECT_ROOT / EMOTION_MODEL_PATH)
        if not os.path.exists(face_path):
            raise FileNotFoundError(f"人脸检测模型权重文件不存在: {face_path}")
        if not os.path.exists(emo_path):
            raise FileNotFoundError(f"表情识别模型权重文件不存在: {emo_path}")
        _face_emotion = FaceEmotionDetector(face_path, emo_path)
    return _face_emotion


def reset_tracker():
    """重置轨迹管理器（切换视频源时用）"""
    global _tracker
    _tracker = TrackStateManager()
    print("[Dependencies] 轨迹管理器已重置")


def reset_cv_engine():
    """完全重置CV引擎"""
    global _detector, _tracker, _roi_manager
    _tracker = TrackStateManager()
    print("[Dependencies] CV引擎已重置")
