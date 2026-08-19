"""
ROI区域管理器 — 加载、判定和可视化感兴趣区域
支持多边形ROI，使用射线法判断点是否在区域内
"""
import yaml
import numpy as np
from pathlib import Path


class ROIManager:
    """ROI（感兴趣区域）管理器

    从YAML配置文件加载多个区域定义，
    提供点在区域内判定和区域属性查询。
    """

    def __init__(self, config_path: str | Path):
        """
        Args:
            config_path: roi_zones.yaml 配置文件路径
        """
        self.config_path = Path(config_path)
        self.zones: dict[str, np.ndarray] = {}         # zone_id → polygon (Nx2)
        self.zone_type: dict[str, str] = {}             # zone_id → "shelf"|"checkout"|"exit"
        self.zone_label: dict[str, str] = {}            # zone_id → 显示名称
        self._base_w = 640
        self._base_h = 480
        self._scale_x = 1.0
        self._scale_y = 1.0
        self._load_from_yaml()

    def set_frame_size(self, width: int, height: int):
        """根据实际帧尺寸缩放 ROI，避免不同分辨率下区域错位。"""
        self._scale_x = width / self._base_w
        self._scale_y = height / self._base_h

    def _scaled_polygon(self, polygon: np.ndarray) -> np.ndarray:
        return np.column_stack([
            polygon[:, 0] * self._scale_x,
            polygon[:, 1] * self._scale_y,
        ])

    # ==================== 加载与保存 ====================

    def _load_from_yaml(self):
        """从YAML文件加载ROI配置"""
        with open(self.config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        zones_cfg = config.get("zones", {})
        for zone_id, zone_info in zones_cfg.items():
            polygon = np.array(zone_info["polygon"], dtype=np.float32)
            self.zones[zone_id] = polygon
            self.zone_type[zone_id] = zone_info.get("type", "shelf")
            self.zone_label[zone_id] = zone_info.get("label", zone_id)

        print(f"[ROIManager] 加载了 {len(self.zones)} 个ROI区域:")
        for zid in self.zones:
            print(f"  - {zid} ({self.zone_type[zid]}): {self.zone_label[zid]}")

    def save_to_yaml(self, path: str | Path | None = None):
        """保存当前ROI配置到YAML文件"""
        target = Path(path) if path else self.config_path
        zones_cfg = {}
        for zone_id in self.zones:
            zones_cfg[zone_id] = {
                "type": self.zone_type[zone_id],
                "label": self.zone_label[zone_id],
                "polygon": self.zones[zone_id].tolist(),
            }
        with open(target, "w", encoding="utf-8") as f:
            yaml.dump({"zones": zones_cfg}, f, allow_unicode=True, sort_keys=False)

    # ==================== 点判定 ====================

    @staticmethod
    def _point_in_polygon(point: tuple[float, float], polygon: np.ndarray) -> bool:
        """射线法判断点是否在多边形内

        Args:
            point: (x, y) 待判定点
            polygon: (N, 2) 多边形顶点坐标

        Returns:
            True 如果点在多边形内
        """
        x, y = point
        n = len(polygon)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            # 射线与多边形边的相交判定
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def get_zone(self, point: tuple[float, float]) -> str | None:
        """判断点落在哪个ROI区域内

        Args:
            point: (x, y) 坐标

        Returns:
            zone_id 或 None（不在任何区域内）
        """
        for zone_id, polygon in self.zones.items():
            polygon = self._scaled_polygon(polygon)
            if self._point_in_polygon(point, polygon):
                return zone_id
        return None

    def get_zones_for_point(self, point: tuple[float, float]) -> list[str]:
        """获取点所在的所有区域（允许重叠）"""
        return [
            zid for zid, poly in self.zones.items()
            if self._point_in_polygon(point, self._scaled_polygon(poly))
        ]

    # ==================== 批量查询 ====================

    def get_zone_type(self, zone_id: str) -> str | None:
        """获取区域类型"""
        return self.zone_type.get(zone_id)

    def get_shelf_zones(self) -> list[str]:
        """获取所有货架类区域ID"""
        return [zid for zid, t in self.zone_type.items() if t == "shelf"]

    def get_zone_by_type(self, zone_type: str) -> list[str]:
        """按类型获取区域ID列表"""
        return [zid for zid, t in self.zone_type.items() if t == zone_type]

    def is_shelf_zone(self, zone_id: str) -> bool:
        """判断是否为货架区域"""
        return self.zone_type.get(zone_id) == "shelf"

    def is_checkout_zone(self, zone_id: str) -> bool:
        """判断是否为收银台区域"""
        return self.zone_type.get(zone_id) == "checkout"

    def is_exit_zone(self, zone_id: str) -> bool:
        """判断是否为出口区域"""
        return self.zone_type.get(zone_id) == "exit"

    # ==================== ROI增删改 ====================

    def add_zone(self, zone_id: str, zone_type: str, label: str, polygon: list[list[float]]):
        """动态添加ROI区域（API用）"""
        self.zones[zone_id] = np.array(polygon, dtype=np.float32)
        self.zone_type[zone_id] = zone_type
        self.zone_label[zone_id] = label

    def remove_zone(self, zone_id: str) -> bool:
        """动态删除ROI区域"""
        if zone_id in self.zones:
            del self.zones[zone_id]
            del self.zone_type[zone_id]
            del self.zone_label[zone_id]
            return True
        return False

    def update_zone_polygon(self, zone_id: str, polygon: list[list[float]]):
        """更新已有区域的坐标"""
        if zone_id in self.zones:
            self.zones[zone_id] = np.array(polygon, dtype=np.float32)

    # ==================== 可视化辅助 ====================

    def draw_zones(self, frame: np.ndarray) -> np.ndarray:
        """在帧上绘制所有ROI区域

        Args:
            frame: BGR图像

        Returns:
            绘制后的图像
        """
        import cv2

        color_map = {
            "shelf": (255, 200, 0),     # 金色
            "checkout": (0, 255, 0),     # 绿色
            "exit": (0, 0, 255),         # 红色
        }

        for zone_id, polygon in self.zones.items():
            polygon = self._scaled_polygon(polygon)
            pts = polygon.astype(np.int32).reshape((-1, 1, 2))
            color = color_map.get(self.zone_type[zone_id], (128, 128, 128))
            cv2.polylines(frame, [pts], isClosed=True, color=color, thickness=2)

            # 标签文字
            cx = int(polygon[:, 0].mean())
            cy = int(polygon[:, 1].mean())
            cv2.putText(
                frame, self.zone_label.get(zone_id, zone_id),
                (cx - 30, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2,
            )

        return frame

    def __repr__(self) -> str:
        return f"ROIManager({len(self.zones)} zones)"
