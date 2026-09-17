"""前端视频链路回归守卫（台账 D13：本地摄像头永久"正在打开"）。

这个 bug 是"**纯前端漏了一步**"：`openSource({kind:'client'})` 没有把上行通道
`/api/ws/client` 打开（老入口 `startClientCamera()` 里是有的，迁到统一入口时漏了），
于是服务端一直等帧、前端每帧都被静默丢弃 → 界面永久停在"正在打开 client…"。

**服务端的任何测试都发现不了它** —— 端点级测试是脚本自己连的 `/api/ws/client`，
服务端看起来完全正常。所以这里做两层：
1. 本文件的**静态守卫**（零依赖，进 CI）：断言关键调用在场、危险写法不在场；
2. `tests/js/test_client_ws.js` 的**行为测试**（需要 node）：
   打桩 WebSocket 后真调 `openSource({kind:'client'})`，验证通道被创建、帧真的发出去。
"""
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

STREAM_JS = PROJECT_ROOT / "frontend-vue" / "public" / "js" / "stream.js"
APP_VUE = PROJECT_ROOT / "frontend-vue" / "src" / "App.vue"
JS_TEST = PROJECT_ROOT / "tests" / "js" / "test_client_ws.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_opensource_starts_client_uplink_for_client_kind():
    """`openSource` 必须在 kind=client 时打开上行通道（这就是被漏掉的那一步）。"""
    src = _read(STREAM_JS)
    fn = src.split("openSource(source)", 1)[1].split("\n    },", 1)[0]
    assert "startClientStream()" in fn, "openSource 又没为 kind=client 打开 /api/ws/client"
    assert "client" in fn, fn[:200]


def test_send_client_frame_is_not_silent():
    """上行通道没建时不能静默丢帧（要计数并给一次可见提示）。"""
    src = _read(STREAM_JS)
    fn = src.split("sendClientFrame(frameBlob)", 1)[1].split("\n    },", 1)[0]
    assert "clientDroppedFrames" in fn, "丢帧又变成静默了"
    assert "updateStatus" in fn, "丢帧没有给用户可见提示"


def test_app_has_capability_safety_net():
    """服务端回执说"正在打开 client"时，前端要再兜一次（防止别处漏调）。"""
    src = _read(APP_VUE)
    assert re.search(r"src\.kind === 'client'[\s\S]{0,120}startClientStream\(\)", src), \
        "App.vue 缺少 kind=client 的兜底调用"


def test_local_camera_failure_is_visible_on_video_panel():
    """本机摄像头失败必须显示在**视频面板**的状态栏，而不是只写进聊天区。"""
    src = _read(APP_VUE)
    # 按"下一个方法名"切，而不是按字符数切 —— 否则函数一变长，断言就会假失败（自己踩过）
    seg = src.split("async startLocalCamera()", 1)[1].split("openFilePicker()", 1)[0]
    assert "navigator.mediaDevices" in seg and "isSecureContext" in seg, \
        "缺少安全上下文检查（http://局域网IP 下会直接 TypeError 且无提示）"
    assert seg.count("window.updateStatus('error'") >= 2, \
        "失败原因没有同时写进状态栏（用户会只看到转圈）"


def test_behavior_test_exists_and_covers_uplink():
    """行为测试必须在仓库里，且覆盖"通道被创建"与"帧被送出"两件事。"""
    assert JS_TEST.is_file(), f"缺少行为测试 {JS_TEST}"
    src = _read(JS_TEST)
    assert "/api/ws/client" in src, "行为测试没有断言上行通道"
    assert "sendClientFrame" in src, "行为测试没有断言帧真的发出去"
