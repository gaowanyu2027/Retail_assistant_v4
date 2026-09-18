"""ROI 缩放与 skill 绑定测试（台账 D16：本地摄像头访客/热度恒为 0）。

背景：`ROIManager` 的多边形以 **640×480** 为基准，按「当前帧尺寸 / 基准」缩放，
而 `set_frame_size()` **只有 VideoProcessor 每帧会调**。
浏览器采帧（本地摄像头）的帧被服务端统一缩放到 **1280×720**，
如果算访客/热度的 skill 用的是**另一个** ROI 实例，它的缩放就永远停在 1.0：

    人站在 1280×720 画面的正中 (640,360) → 落在所有多边形之外 → 访客/热度恒为 0

修复方式：让 skill 与 processor **共用同一个 ROI 实例**（`start_processing(use_roi=…)`）。
本测试同时钉住"错误形态"和"正确形态"，避免回归。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import LOCAL_ROI_CONFIG_PATH, ROI_CONFIG_PATH  # noqa: E402
from cv_engine.roi_manager import ROIManager  # noqa: E402
from skills.skill_popularity import PopularitySkill  # noqa: E402

FRAME = (1280, 720)          # 客户端帧被服务端统一缩放到的尺寸
CENTER = (640, 360)          # 画面正中


def _roi(path: str) -> ROIManager:
    return ROIManager(str(PROJECT_ROOT / path))


def test_stale_scale_misses_everything():
    """错误形态（修复前）：ROI 没同步帧尺寸时，画面正中也判不出区域。

    这条**故意断言失败形态**：它记录的是"如果将来有人又让 skill 用独立 ROI 实例，
    现象会是什么样"，比只测正确路径更能防止回归。
    """
    roi = _roi(LOCAL_ROI_CONFIG_PATH)
    assert roi.get_zone(CENTER) is None, "缩放未同步时不应命中任何区域（此用例用于记录该形态）"


def test_synced_scale_hits_a_shelf_zone():
    """正确形态：同步到实际帧尺寸后，画面正中应落在某个货架区。"""
    for path in (LOCAL_ROI_CONFIG_PATH, ROI_CONFIG_PATH):
        roi = _roi(path)
        roi.set_frame_size(*FRAME)
        hit = roi.get_zone(CENTER)
        assert hit is not None, f"{path}: 同步缩放后正中应命中区域"
        assert roi.is_shelf_zone(hit), f"{path}: 正中应落在货架区，实际 {hit}"


def test_both_roi_files_agree_on_center():
    """两套 ROI 布局应基本一致（本地那份是从门店那份派生的），否则换源会"区域跳变"。"""
    local, server = _roi(LOCAL_ROI_CONFIG_PATH), _roi(ROI_CONFIG_PATH)
    local.set_frame_size(*FRAME)
    server.set_frame_size(*FRAME)
    assert local.get_zone(CENTER) == server.get_zone(CENTER)


def test_skill_reads_roi_live_so_repointing_works():
    """skill 必须**实时**读 `self.roi_manager`（不能缓存区域集合），否则换引用不生效。"""
    a = _roi(LOCAL_ROI_CONFIG_PATH)
    b = _roi(ROI_CONFIG_PATH)
    a.set_frame_size(*FRAME)
    b.set_frame_size(*FRAME)

    skill = PopularitySkill(a)
    assert skill.roi_manager is a
    before = skill.roi_manager.get_zone(CENTER)

    skill.roi_manager = b                      # 换源时就是这么切的（见 stream.py 的 use_roi）
    assert skill.roi_manager is b, "skill 没换引用 = 修复不会生效"
    assert skill.roi_manager.get_zone(CENTER) == before


def test_processor_updates_the_same_manager_it_was_given():
    """结构性断言：VideoProcessor 把 set_frame_size 调在**传入的那个** ROI 上。

    这正是"共用实例"能修掉缩放问题的原因——所以把这条契约钉住。

    实现说明：这里用 **AST 直接读源码**，而不是 `import VideoProcessor` 再
    `inspect.getsource()`。因为 `cv_engine/video_processor.py` 在模块级 import
    cv2 + ultralytics（=torch），而 CI 的"最小依赖"单测 job 里没有这些 ——
    原来那种写法会让本文件在 CI 里直接导入失败（实测，见 改进记录"CI 首跑"）。
    断言强度不变：验的是**源码里的调用契约**，本来就不需要真的跑起来。
    """
    import ast
    from pathlib import Path

    src_path = Path(__file__).resolve().parents[1] / "cv_engine" / "video_processor.py"
    source = src_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    func_src = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "process_frame":
            seg = ast.get_source_segment(source, node)
            if seg:
                func_src = seg
                break
        if isinstance(node, ast.ClassDef) and node.name == "VideoProcessor":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "process_frame":
                    func_src = ast.get_source_segment(source, sub)
                    break
    assert func_src, "没找到 VideoProcessor.process_frame（结构变了？）"
    assert "roi_manager.set_frame_size" in func_src, \
        "process_frame 不再同步帧尺寸？那共用实例也救不了缩放"
    assert "self.roi_manager" in func_src, "processor 应该用传入的 ROI 实例"
