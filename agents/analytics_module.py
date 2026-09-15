"""
分析模块抽象 — 模块化"摄像头按需加载"的基础

每个分析模块（货架热度 / 异常检测 / 客流计数 / 表情体验…）被包装成
AnalyticsModule：有名称、候选摄像头类型白名单、以及 get_stats()（供 Agent 工具调用）。

摄像头「标签(type)」决定候选池，config 决定实际加载哪些模块；加载即注册 Agent 工具。
先复用现有 skill 作为模块实例（阶段一共享数据），后续阶段再 multi-instance 每个摄像头独立。

预留新模块位：empty_shelf(空货架补货)/footfall(客流计数)/passerby_conv(路过转化)/
queue_monitor(排队体验) 等，后续按业务实现后 register_module 即可，无需改注册表。
"""
import json
from typing import Any, Callable


class AnalyticsModule:
    """分析模块基类。

    name: 模块ID（如 shelf_heat）
    type_hint: 该模块适用的摄像头类型候选白名单（如 ["indoor_shelf"]）

    每个模块独立实例化（每摄像头一份），状态彼此隔离（数据隔离核心）。
    """

    name: str = ""
    type_hint: list[str] = []

    def process(self, tracks: list, frame_id: int, fps: float, timestamp: float | None = None,
                current_hour: str | None = None) -> None:
        """喂一帧轨迹数据（视频管线每帧调用）。无状态模块可 no-op。"""
        return None

    def get_stats(self, **kwargs) -> dict:
        """模块统计快照（供 Agent 工具查询）。"""
        raise NotImplementedError

    def reset(self) -> None:
        """重置模块状态（切换视频源时）。"""


# ==================== 模块工厂注册表 ====================

# 工厂签名：工厂(roi_manager) -> AnalyticsModule（每摄像头独立实例，用该镜头 ROI）
_MODULE_FACTORIES: dict[str, Callable[[Any], AnalyticsModule]] = {}
_MODULE_META: dict[str, list[str]] = {}


def register_module(name: str, type_hint: list[str],
                    factory: Callable[[Any], AnalyticsModule]) -> None:
    """注册一个模块工厂。工厂(roi_manager) 返回该摄像头的模块实例（数据独立）。"""
    _MODULE_FACTORIES[name] = factory
    _MODULE_META[name] = type_hint


def create_module(name: str, roi_manager: Any = None) -> AnalyticsModule | None:
    """按名称实例化一个模块（传入该摄像头的 ROI，返回独立实例；未注册返回 None）。"""
    factory = _MODULE_FACTORIES.get(name)
    return factory(roi_manager) if factory else None


def module_type_hint(name: str) -> list[str]:
    """返回模块的候选摄像头类型白名单。"""
    return _MODULE_META.get(name, [])


def list_registered_modules() -> list[str]:
    return list(_MODULE_FACTORIES.keys())


# 摄像头类型 → 候选模块池（标签决定"能装哪些模块"，config 决定"装哪些"）
# 注意：候选池应只含【已注册】的模块；计划中未实现的（passerby_conv/hourly_traffic/
# queue_monitor 等）不列入，避免配置到未注册模块导致静默加载 0 个。
_TYPE_CANDIDATES: dict[str, list[str]] = {
    "indoor_shelf": ["shelf_heat", "anomaly_detect", "empty_shelf"],
    "entrance": ["footfall"],          # passerby_conv/hourly_traffic 计划中
    "checkout": ["emotion_experience"],  # queue_monitor 计划中
}


def candidates_for_type(cam_type: str) -> list[str]:
    """返回某摄像头类型允许加载的模块池。"""
    return _TYPE_CANDIDATES.get(cam_type, [])
