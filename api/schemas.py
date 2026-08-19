"""
API 数据模型 (Pydantic Schemas)
定义所有请求/响应/WebSocket消息的数据结构
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Any
from datetime import datetime


# ==================== 请求模型 ====================

class QueryRequest(BaseModel):
    """自然语言查询请求"""
    question: str = Field(..., min_length=1, max_length=500, description="用户问题")
    context: dict[str, Any] | None = Field(default=None, description="可选的会话上下文")
    session_id: str = Field(default="default", description="会话ID")


class VoiceCommandRequest(BaseModel):
    """语音助手命令请求"""
    text: str = Field(..., min_length=1, max_length=500, description="语音识别文本")
    session_id: str = Field(default="voice_default", max_length=100, description="会话ID")


class ROIUpdateRequest(BaseModel):
    """ROI区域更新请求"""
    zone_id: str
    zone_type: str = Field(..., pattern=r"^(shelf|checkout|exit)$")
    label: str
    polygon: list[list[float]]  # [[x1,y1], [x2,y2], ...]


class VideoSourceRequest(BaseModel):
    """视频源切换请求"""
    source: str = Field(..., pattern=r"^(webcam|file|image_sequence)$")
    camera_id: int = Field(default=0)
    file_path: str | None = None
    img_dir: str | None = None


# ==================== 响应模型 ====================

class QueryResponse(BaseModel):
    """自然语言查询响应"""
    answer: str = Field(..., description="LLM生成的自然语言回答")
    intent: str = Field(..., description="popularity | anomaly | both | general")
    confidence: float = Field(..., description="意图分类置信度")
    data: dict[str, Any] = Field(default_factory=dict, description="结构化数据")
    alerts: list[dict[str, Any]] = Field(default_factory=list, description="异常告警")
    suggestions: list[str] = Field(default_factory=list, description="建议操作")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class AlertItem(BaseModel):
    """单条异常告警"""
    person_id: int
    track_id: int
    frame_id: int
    score: int
    level: str               # "watch" | "high"
    reasons: list[str]
    timestamp: str
    snapshot_url: str | None = None


class ZoneStatsResponse(BaseModel):
    """单个区域统计"""
    zone_id: str
    zone_label: str
    count: int
    current_visitors: int
    avg_dwell_seconds: float
    max_dwell_seconds: float
    min_dwell_seconds: float
    total_dwell_seconds: float
    hourly_counts: dict[int, int] = Field(default_factory=dict)


class PopularityReportResponse(BaseModel):
    """货架热度报表"""
    zones: dict[str, ZoneStatsResponse]
    top_zone: str | None
    total_visitors: int
    timestamp: str


class AnomalyReportResponse(BaseModel):
    """异常行为报表"""
    total_alerts: int
    high_risk_count: int
    watch_count: int
    high_risk: list[dict[str, Any]]
    watch_list: list[dict[str, Any]]


class DashboardSnapshot(BaseModel):
    """仪表盘全局快照"""
    timestamp: str
    popularity: dict[str, Any]
    anomaly: dict[str, Any]
    fps: float = 0.0
    active_tracks: int = 0
    status: str = "idle"


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str
    gpu_available: bool
    device: str
    model_loaded: bool
    video_source: str
    uptime_seconds: float


# ==================== WebSocket 消息模型 ====================

class WSFrameMessage(BaseModel):
    """WebSocket 视频帧推送消息"""
    type: str = "frame"
    frame: str = Field(..., description="Base64编码的JPEG图像")
    frame_id: int
    timestamp: float
    tracks: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)


class WSEventMessage(BaseModel):
    """WebSocket 事件推送消息"""
    type: str = "event"
    event_type: str  # "crowd_gathering" | "trajectory_anomaly" | "alert" | "interest_detected"
    data: dict[str, Any]


class WSDashboardMessage(BaseModel):
    """WebSocket 仪表盘更新消息"""
    type: str = "dashboard_update"
    shelf_heat: dict[str, int] = Field(default_factory=dict)
    active_suspicious: int = 0
    fps: float = 0.0
    total_visitors: int = 0
    active_tracks: int = 0
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class WSStatusMessage(BaseModel):
    """WebSocket 状态消息"""
    type: str = "status"
    status: str  # "processing" | "paused" | "stopped" | "error"
    message: str
