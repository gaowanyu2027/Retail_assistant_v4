"""摄像头模块注册表：候选池自洽、加载校验、运行时开关。

关键不变量（AGENTS.md 提到的"配置写了却不生效"类问题）：

- `_TYPE_CANDIDATES` 里的每个模块都必须**已注册**；否则配置写进去会静默加载 0 个；
- `Camera.add_module` 只允许候选池内的模块，重复添加返回 False；
- 关闭的模块 `module_stats` 返回 `{"status": "not_enabled"}`，而不是旧数据。

不调用 `load_from_yaml`（其 `_ensure_roi` 会牵出 `api.dependencies` 的重依赖），
用假 ROI 直接构造 Camera。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents import analytics_module as am  # noqa: E402
from agents import module_registry as mr  # noqa: E402


class FakeROI:
    def __init__(self, shelf=("shelf_1", "shelf_2")):
        self._shelf = list(shelf)
        self.zone_label = {z: z for z in self._shelf}

    def get_shelf_zones(self):
        return list(self._shelf)

    def get_zone_by_type(self, zone_type):
        return []


def test_candidate_pools_only_contain_registered_modules():
    registered = set(am.list_registered_modules())
    assert {"shelf_heat", "anomaly_detect", "emotion_experience",
            "footfall", "empty_shelf"} <= registered
    for cam_type, pool in am._TYPE_CANDIDATES.items():
        assert pool, f"{cam_type} 的候选池不能为空"
        missing = [m for m in pool if m not in registered]
        assert not missing, f"{cam_type} 候选池含未注册模块: {missing}"


def test_type_candidate_mapping():
    assert set(am.candidates_for_type("indoor_shelf")) == {
        "shelf_heat", "anomaly_detect", "empty_shelf"}
    assert am.candidates_for_type("entrance") == ["footfall"]
    assert am.candidates_for_type("checkout") == ["emotion_experience"]
    assert am.candidates_for_type("unknown_type") == []


def test_camera_add_module_validates_candidate_pool_and_duplicates():
    cam = mr.Camera("cam1", "一号", "indoor_shelf", "webcam", [],
                    roi_manager=FakeROI())
    assert cam.candidates == am.candidates_for_type("indoor_shelf")

    assert cam.add_module("anomaly_detect") is True
    assert cam.add_module("anomaly_detect") is False, "重复添加不报错、返回 False"
    assert cam.add_module("footfall", raise_on_invalid=False) is False

    try:
        cam.add_module("footfall")
    except ValueError as e:
        assert "不支持模块 footfall" in str(e)
    else:
        raise AssertionError("候选池外模块应抛 ValueError")


def test_camera_enable_disable_and_stats():
    cam = mr.Camera("cam1", "一号", "indoor_shelf", "webcam", [],
                    roi_manager=FakeROI())
    cam.add_module("shelf_heat")
    assert cam.enabled_modules() == ["shelf_heat"]
    stats = cam.module_stats("shelf_heat")
    assert isinstance(stats, dict) and stats.get("status") != "not_enabled"

    cam.set_enabled("shelf_heat", False)
    assert cam.enabled_modules() == []
    assert cam.module_stats("shelf_heat") == {
        "status": "not_enabled", "module": "shelf_heat", "camera": "cam1"}

    cam.set_enabled("shelf_heat", True)
    assert cam.enabled_modules() == ["shelf_heat"]

    cam.remove_module("shelf_heat")
    assert cam.module_stats("shelf_heat")["status"] == "not_enabled"


def test_registry_unknown_camera_is_reported_not_raised():
    reg = mr.ModuleRegistry()
    assert reg.add_module("nope", "shelf_heat") == {
        "ok": False, "error": "摄像头 nope 不存在"}
    assert reg.remove_module("nope", "shelf_heat")["ok"] is False
    assert reg.set_module_enabled("nope", "shelf_heat", True)["ok"] is False
    stats = reg.get_module_stats("nope", "shelf_heat")
    assert stats["status"] == "error" and "不存在" in stats["error"]


def test_registry_add_enable_remove_on_known_camera():
    reg = mr.ModuleRegistry()
    cam = mr.Camera("cam1", "一号", "indoor_shelf", "webcam", [],
                    roi_manager=FakeROI())
    reg._cameras["cam1"] = cam

    added = reg.add_module("cam1", "anomaly_detect")
    assert added["ok"] is True and added["added"] is True
    assert "anomaly_detect" in added["camera"]["enabled_modules"]

    again = reg.add_module("cam1", "anomaly_detect")
    assert again["ok"] is True and again["added"] is False

    invalid = reg.add_module("cam1", "footfall")
    assert invalid["ok"] is False and "不支持模块" in invalid["error"]

    off = reg.set_module_enabled("cam1", "anomaly_detect", False)
    assert off["ok"] is True and off["camera"]["enabled_modules"] == []
    assert reg.get_module_stats("cam1", "anomaly_detect")["status"] == "not_enabled"

    removed = reg.remove_module("cam1", "anomaly_detect")
    assert removed["ok"] is True
    assert reg.get_module_stats("cam1", "anomaly_detect")["status"] == "not_enabled"


def test_active_camera_binding_and_feed():
    reg = mr.ModuleRegistry()
    cam = mr.Camera("cam1", "一号", "indoor_shelf", "webcam", [],
                    roi_manager=FakeROI())
    cam.add_module("anomaly_detect")
    reg._cameras["cam1"] = cam

    assert reg.active_camera_id is None
    reg.feed_current_frame([], frame_id=1, fps=30)  # 未绑定：静默跳过

    reg.set_active_camera("cam1")
    assert reg.active_camera_id == "cam1"
    reg.feed_current_frame([], frame_id=2, fps=30)  # 不应抛异常

    summary = cam.module_stats("anomaly_detect")
    assert summary["total_alerts_cumulative"] == 0
