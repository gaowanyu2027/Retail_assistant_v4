"""
模块注册表 — 摄像头按需加载分析模块的核心

职责：
- 按 config/cameras.yaml 加载摄像头配置（标签 type 决定候选模块池）
- 校验 modules ⊆ 候选池（标签限定、防配错）
- 运行时增删/开关模块（供前端"显性按钮"调接口，不改代码、不重启）
- 把每个摄像头启用的模块能力暴露为 Agent 工具（get_module_stats）

阶段一：模块 get_stats 复用现有 skill 单例（共享数据，重在模块化骨架与接口）。
阶段二：multi-instance 每摄像头独立 roi/detector/skill，数据真正按摄像头隔离。
"""
import json
import threading

from agents.analytics_module import (
    AnalyticsModule,
    register_module,
    create_module,
    module_type_hint,
    candidates_for_type,
    list_registered_modules,
)


# ==================== 内置模块注册（数据隔离：每摄像头独立 skill 实例） ====================

def _shelf_heat_maker(roi_manager):
    from skills.skill_popularity import PopularitySkill

    class M(AnalyticsModule):
        name = "shelf_heat"
        type_hint = ["indoor_shelf"]

        def __init__(self, roi):
            self._skill = PopularitySkill(roi_manager=roi)

        def process(self, tracks, frame_id, fps, timestamp=None, current_hour=None):
            self._skill.process(tracks, frame_id, fps, timestamp, current_hour)

        def get_stats(self, **kw):
            return self._skill.get_stats()

    return M(roi_manager)


def _anomaly_maker(roi_manager):
    from skills.skill_anomaly import AnomalySkill

    class M(AnalyticsModule):
        name = "anomaly_detect"
        type_hint = ["indoor_shelf"]

        def __init__(self, roi):
            self._skill = AnomalySkill(roi_manager=roi)

        def process(self, tracks, frame_id, fps, timestamp=None, current_hour=None):
            self._skill.process(tracks, frame_id, fps, timestamp or "")

        def get_stats(self, **kw):
            return self._skill.get_alert_summary()

    return M(roi_manager)


def _emotion_maker(roi_manager):
    from skills.skill_emotion import SkillEmotion

    class M(AnalyticsModule):
        name = "emotion_experience"
        type_hint = ["checkout"]

        def __init__(self, roi):
            self._skill = SkillEmotion()

        def process(self, tracks, frame_id, fps, timestamp=None, current_hour=None):
            pass  # 表情模块由摄像头抽帧喂入（后续接 face_emotion）

        def get_stats(self, **kw):
            return self._skill.get_stats()

    return M(roi_manager)


def _register_builtin_modules():
    register_module("shelf_heat", ["indoor_shelf"], _shelf_heat_maker)
    register_module("anomaly_detect", ["indoor_shelf"], _anomaly_maker)
    register_module("emotion_experience", ["checkout"], _emotion_maker)
    # 阶段二新模块：客流计数 / 空货架（导入即注册，填进候选池）
    import skills.skill_new_modules  # noqa: F401


_register_builtin_modules()


# ==================== 摄像头配置 ====================

class Camera:
    def __init__(self, cam_id: str, name: str, cam_type: str, source: str, modules: list[str],
                 roi_manager=None):
        self.id = cam_id
        self.name = name
        self.type = cam_type
        self.source = source
        # 独立 ROI（每镜头一套区域定义）；未指定则用全局 ROI
        self.roi_manager = roi_manager
        self.enabled = True
        # 已加载模块实例 {mod_name: AnalyticsModule}（每镜头独立，数据隔离）
        self._modules: dict[str, AnalyticsModule] = {}
        self._module_enabled: dict[str, bool] = {}
        self._lock = threading.Lock()
        for m in modules:
            self.add_module(m)

    def _ensure_roi(self):
        """确保有 ROI；未设置则加载全局 ROI 单例。"""
        if self.roi_manager is None:
            from api.dependencies import get_roi_manager
            self.roi_manager = get_roi_manager()
        return self.roi_manager

    @property
    def candidates(self) -> list[str]:
        return candidates_for_type(self.type)

    def add_module(self, mod_name: str, raise_on_invalid=True):
        """加载一个模块实例（校验候选池 + 独立 skill + 独立 ROI）。"""
        with self._lock:
            if mod_name not in self.candidates:
                if raise_on_invalid:
                    raise ValueError(f"摄像头类型{self.type}不支持模块 {mod_name}（候选: {self.candidates}）")
                return False
            if mod_name in self._modules:
                return False
            mod = create_module(mod_name, self._ensure_roi())
            if mod is None:
                raise ValueError(f"模块 {mod_name} 未注册")
            self._modules[mod_name] = mod
            self._module_enabled[mod_name] = True
            return True

    def process_frame(self, tracks: list, frame_id: int, fps: float,
                      timestamp: float | None = None, current_hour: str | None = None):
        """喂一帧轨迹给本摄像头所有启用模块（视频管线每帧调用，实现数据隔离）。"""
        with self._lock:
            for mod_name, mod in self._modules.items():
                if self._module_enabled.get(mod_name, False):
                    try:
                        mod.process(tracks, frame_id, fps, timestamp, current_hour)
                    except Exception as e:
                        print(f"[Camera {self.id}] 模块 {mod_name} 处理失败: {e}")

    def remove_module(self, mod_name: str):
        with self._lock:
            self._modules.pop(mod_name, None)
            self._module_enabled.pop(mod_name, None)

    def set_enabled(self, mod_name: str, enabled: bool):
        with self._lock:
            if mod_name in self._modules:
                self._module_enabled[mod_name] = enabled

    def enabled_modules(self) -> list[str]:
        with self._lock:
            return [m for m, e in self._module_enabled.items() if e]

    def module_stats(self, mod_name: str) -> dict:
        with self._lock:
            mod = self._modules.get(mod_name)
            enabled = self._module_enabled.get(mod_name, False)
        if mod is None or not enabled:
            return {"status": "not_enabled", "module": mod_name, "camera": self.id}
        try:
            return mod.get_stats()
        except Exception as e:
            return {"status": "error", "module": mod_name, "error": str(e)}

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "type": self.type,
            "source": self.source,
            "candidates": self.candidates,
            "enabled_modules": self.enabled_modules(),
        }


class ModuleRegistry:
    def __init__(self):
        self._cameras: dict[str, Camera] = {}
        self._lock = threading.Lock()
        self._active_camera: str | None = None  # 视频输出绑定；None 表示未绑定（feed 时跳过）

    def load_from_yaml(self, yaml_path: str) -> int:
        import yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        cameras = cfg.get("cameras", {})
        for cam_id, c in cameras.items():
            cam_type = c.get("type", "indoor_shelf")
            mods = c.get("modules", [])
            # 校验：候选池内 且 已注册；未注册/候选池外 → 跳过（阶段二实现后自动生效）
            valid = [m for m in mods
                     if m in candidates_for_type(cam_type) and m in list_registered_modules()]
            warn = [m for m in mods if m not in valid]
            if warn:
                print(f"[ModuleRegistry] 摄像头{cam_id} 跳过候选池外/未注册模块: {warn}")
            self._cameras[cam_id] = Camera(cam_id, c.get("name", cam_id), cam_type,
                                           c.get("source", "webcam"), valid)
        print(f"[ModuleRegistry] 加载 {len(self._cameras)} 个摄像头配置")
        return len(self._cameras)

    # ---- 查询 ----
    def list_cameras(self) -> list[dict]:
        return [c.to_dict() for c in self._cameras.values()]

    def get_camera(self, cam_id: str) -> Camera | None:
        return self._cameras.get(cam_id)

    def candidate_modules(self, cam_type: str) -> list[str]:
        return candidates_for_type(cam_type)

    def list_available_modules(self) -> list[dict]:
        return [{"name": n, "type_hint": module_type_hint(n)} for n in list_registered_modules()]

    # ---- 运行时增删/开关（前端按钮 → 接口 → 这些方法） ----
    def add_module(self, cam_id: str, mod_name: str) -> dict:
        cam = self._cameras.get(cam_id)
        if cam is None:
            return {"ok": False, "error": f"摄像头 {cam_id} 不存在"}
        try:
            ok = cam.add_module(mod_name)
            return {"ok": True, "added": ok, "camera": cam.to_dict()}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def remove_module(self, cam_id: str, mod_name: str) -> dict:
        cam = self._cameras.get(cam_id)
        if cam is None:
            return {"ok": False, "error": f"摄像头 {cam_id} 不存在"}
        cam.remove_module(mod_name)
        return {"ok": True, "removed": mod_name, "camera": cam.to_dict()}

    def set_module_enabled(self, cam_id: str, mod_name: str, enabled: bool) -> dict:
        cam = self._cameras.get(cam_id)
        if cam is None:
            return {"ok": False, "error": f"摄像头 {cam_id} 不存在"}
        cam.set_enabled(mod_name, enabled)
        return {"ok": True, "camera": cam.to_dict()}

    # ---- 统计 ----
    def get_module_stats(self, cam_id: str, mod_name: str) -> dict:
        cam = self._cameras.get(cam_id)
        if cam is None:
            return {"status": "error", "error": f"摄像头 {cam_id} 不存在"}
        return cam.module_stats(mod_name)

    # ---- 视频管线绑定：喂数据给"当前活跃摄像头" ----
    def set_active_camera(self, cam_id: str) -> None:
        """将当前视频输出绑定到某个逻辑摄像头（多路视频扩展用）。"""
        self._active_camera = cam_id

    @property
    def active_camera_id(self) -> str | None:
        return self._active_camera

    def feed_current_frame(self, tracks: list, frame_id: int, fps: float,
                           timestamp: float | None = None, current_hour: str | None = None) -> None:
        """视频管线每帧调用：把轨迹喂给当前绑定的摄像头模块（数据隔离）。"""
        cam = self._cameras.get(self._active_camera) if self._active_camera else None
        if cam is not None and cam.enabled:
            cam.process_frame(tracks, frame_id, fps, timestamp, current_hour)

    def scan_cameras(self) -> list[dict]:
        """模拟扫描可用摄像头（阶段一：返回配置中的摄像头；阶段二用 OpenCV/RTSP 实际探测）。"""
        return self.list_cameras()

    def save_to_yaml(self, yaml_path: str | None = None) -> dict:
        """把当前运行时配置（摄像头 + 已加载模块）持久化回 cameras.yaml（重启不丢）。

        运行时增删模块/添加摄像头后调用，保证配置落盘。
        """
        from config.settings import PROJECT_ROOT
        yaml_path = yaml_path or str(PROJECT_ROOT / "config" / "cameras.yaml")
        payload = {"cameras": {}}
        for c in self._cameras.values():
            payload["cameras"][c.id] = {
                "name": c.name, "type": c.type, "source": c.source,
                "modules": list(c._modules.keys()),   # 已加载（含 enabled 的），落盘后重启加载
            }
        import yaml as _yaml
        with open(yaml_path, "w", encoding="utf-8") as f:
            _yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
        return {"ok": True, "saved": len(payload["cameras"]), "path": yaml_path}


# ==================== 全局单例 ====================

_registry: ModuleRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ModuleRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                from config.settings import PROJECT_ROOT
                _registry = ModuleRegistry()
                _registry.load_from_yaml(str(PROJECT_ROOT / "config" / "cameras.yaml"))
    return _registry


def build_module_tool() -> "object":
    """生成一个通用模块查询工具（供 master_agent 动态注册）。"""
    from langchain_core.tools import tool

    @tool
    def get_module_stats(module: str, camera_id: str) -> str:
        """查询某摄像头某分析模块的统计（如 shelf_heat/anomaly_detect/emotion_experience）。

        参数 module: 模块名（shelf_heat / anomaly_detect / emotion_experience）
        参数 camera_id: 摄像头ID（如 cam_in_01）
        """
        reg = get_registry()
        return json.dumps(reg.get_module_stats(camera_id, module), ensure_ascii=False, default=str)

    return get_module_stats
