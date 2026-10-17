"""视频会话语义（混合架构迁移 · S3 冻结）：事件日志 / 观看者仲裁 / 停止策略 / 上行信封。

为什么盯住这一层：视频数据面迁 Go 后，**连接与扇出在 Go、会话语义仍在 Python**。
Go 侧要复刻的正是这里的行为（丢帧、控制事件的确定投递、"最后一个观看者离开才停"、
本机摄像头归属），所以必须先把它们钉成断言 —— 否则 Go 那边只能靠读代码猜。

覆盖：
  1. 事件日志与游标：新读者从最新帧起、控制事件不丢且与帧全局有序、积压上限=帧环；
  2. 会话停止策略：最后一个观看者离开才停（多人下是对迁移前"任一断开即全局停"的
     **有意改进**）、停止后不再补发迟到帧、终止消息确定投递（含 framed_tail 判据）；
  3. 本机摄像头归属：第二人拒绝且**不影响正在跑的会话**、幽灵归属回收、无首帧终止即释放；
  4. 动作回执（ActionOutcome）：文案、条数与顺序、replaced/clear/framed_tail 语义；
  5. 打开超时看门狗、Go 上行流断开宽限；
  6. 上行信封与 contracts/ws_messages.json 逐字节一致（跨语言 golden）。

依赖约定：本文件只依赖 stdlib + fastapi/starlette + vision.session + ws_protocol
（`vision/session.py` 已把 cv2/numpy 收进函数内延迟导入，故 CI 最小依赖集里可导入）。
用**新建的 SessionManager 实例**做测试，不碰进程内单例 MANAGER —— run_tests.py 在同一
进程里跑全部用例，用例之间必须互不影响。
"""
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from starlette.websockets import WebSocketState  # noqa: E402

import vision.session as vs  # noqa: E402
import ws_protocol as wsp  # noqa: E402
from api.routes import vision_internal as vi  # noqa: E402
from api.routes.stream import _drain  # noqa: E402
from config.settings import VIDEO_OPEN_TIMEOUT_SECONDS  # noqa: E402
from vision.session import ActionOutcome, ControlEvent, FrameEvent, SessionManager, VideoSession  # noqa: E402

FIXTURE_PATH = PROJECT_ROOT / "contracts" / "ws_messages.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class _Conn:
    """最小 WS 替身：`ws_alive` 只看 `client_state`。"""

    def __init__(self, state=WebSocketState.CONNECTED):
        self.client_state = state


def _mk_manager() -> SessionManager:
    return SessionManager()


def _mk_session(mgr: SessionManager, *, running: bool = True, kind: str = "file",
                echo: dict | None = None, requester: str | None = None) -> VideoSession:
    """直接造一个会话并挂到 manager 上（**不**走 start_session：那会去加载模型）。"""
    gen = mgr.log.new_gen()
    sess = VideoSession(mgr, "s1", gen, "retail", kind, {},
                        echo if echo is not None else {"kind": "file", "path": "/x.mp4"},
                        requester)
    sess.running = running
    mgr.session = sess
    return sess


def _publish_frames(mgr: SessionManager, gen: int, frame_ids) -> None:
    for fid in frame_ids:
        mgr.log.publish_frame(gen, fid, wsp.dumps_message({"type": "frame", "frame_id": fid}),
                              b"\xff\xd8JPEG")


def _drain_cursor(cursor) -> list:
    out = []
    while True:
        ev = cursor.next(0.0)
        if ev is None:
            return out
        out.append(ev)


def _msgs(texts) -> list:
    return [json.loads(t) for t in texts]


def _parse_envelope(buf: bytes) -> dict:
    """上行信封 → dict（与 Go 侧解析器同一套字段）。

    头 18 字节：magic(4) | ver(1) | kind(1) | frame_id(4) | metaLen(4) | jpegLen(4)。
    """
    assert buf[:4] == b"DSHF", f"魔数不对：{buf[:4]!r}"
    assert buf[4] == vi._VERSION
    meta_len = int.from_bytes(buf[10:14], "big")
    jpeg_len = int.from_bytes(buf[14:18], "big")
    meta = buf[18:18 + meta_len]
    jpeg = buf[18 + meta_len:18 + meta_len + jpeg_len]
    assert len(buf) == 18 + meta_len + jpeg_len, "信封长度与两个长度字段不自洽"
    return {"kind": buf[5], "frame_id": int.from_bytes(buf[6:10], "big"),
            "meta_utf8": meta.decode("utf-8"), "jpeg": jpeg}


# ==================== 事件日志与游标 ====================

def test_new_cursor_delivers_latest_frame_once():
    """新观看者立刻看到画面：起点=最新帧（投递一次），更早的帧与历史控制事件都不重放。"""
    mgr = _mk_manager()
    gen = mgr.log.new_gen()
    mgr.log.publish_control(gen, [wsp.dumps_message({"type": "status", "status": "stopped"})])
    _publish_frames(mgr, gen, (1, 2, 3))

    evs = _drain_cursor(mgr.log.cursor())
    assert [e.frame_id for e in evs] == [3], f"新游标应只收到最新一帧，实际 {evs}"
    assert not any(isinstance(e, ControlEvent) for e in evs), "历史控制事件不该补发"


def test_frame_backlog_is_bounded_by_ring_and_catches_up():
    """帧环即积压上限：游标落后超过环容量时，环外的旧帧被丢弃，随后追上最新帧。"""
    mgr = _mk_manager()
    gen = mgr.log.new_gen()
    _publish_frames(mgr, gen, (1, 2, 3, 4))
    cursor = mgr.log.cursor()                     # 停在 4
    _publish_frames(mgr, gen, (5, 6, 7, 8, 9, 10, 11, 12))

    got = [e.frame_id for e in _drain_cursor(cursor) if isinstance(e, FrameEvent)]
    assert got == [9, 10, 11, 12], f"积压应被环容量截断并追上最新，实际 {got}"
    assert len(got) == vs._FRAME_RING


def test_control_events_are_ordered_with_frames_and_never_lost():
    """控制事件与帧事件按 seq 全局有序；控制事件不因读得慢而丢（环足够大）。"""
    mgr = _mk_manager()
    gen = mgr.log.new_gen()
    cursor = mgr.log.cursor()
    _publish_frames(mgr, gen, (1, 2))
    mgr.log.publish_control(gen, ["A"])
    _publish_frames(mgr, gen, (3,))
    mgr.log.publish_control(gen, ["B", "C"])

    kinds = ["F" + str(e.frame_id) if isinstance(e, FrameEvent) else "C" + "+".join(e.texts)
             for e in _drain_cursor(cursor)]
    assert kinds == ["F1", "F2", "CA", "F3", "CB+C"], kinds


def test_stop_publishes_tail_with_clear_and_except():
    """停止：`texts` 为空、`framed_tail` 是"视频流已停止"、`clear=True`、发起人除外。"""
    mgr = _mk_manager()
    sess = _mk_session(mgr)
    cursor = mgr.log.cursor()
    tail = mgr.stop_session(requester="vA", reason="stop")

    assert tail == _fixture()["messages"]["stream_stopped_tail"]
    assert sess.running is False and sess.ended is True
    evs = _drain_cursor(cursor)
    assert len(evs) == 1 and isinstance(evs[0], ControlEvent), evs
    ev = evs[0]
    assert ev.texts == [] and ev.framed_tail == tail
    assert ev.clear is True and ev.gen == sess.gen
    assert ev.except_viewer == "vA", "发起人自己走回执，不该再收一遍"
    # 已经停过一次：返回 None（调用方据此跳过回执里的收尾消息）
    assert mgr.stop_session(requester="vA", reason="stop") is None


def test_frames_are_not_published_after_stop():
    """停止后处理线程再推的帧一律丢弃（迁移前会在"已停止"之后又冒出一帧）。"""
    mgr = _mk_manager()
    sess = _mk_session(mgr)
    mgr.stop_session(requester=None, reason="stop")
    cursor = mgr.log.cursor()
    sess.publish_frame(1, {"frame_id": 1}, b"\xff\xd8")

    assert mgr.log.latest is None
    assert _drain_cursor(cursor) == []


def test_late_frame_racing_stop_does_not_resurrect_latest():
    """停止与出帧竞态：处理线程已越过 running 检查、stop 之后才发布的那一帧也要丢。

    真实触发路径：处理线程读取 `sess.running` 为 True → 停止命令把会话置停并发出
    clear 事件 → 处理线程才走到 `log.publish_frame`。若放行，这一帧会排在 clear
    之后并复活 `_latest`，此后新观看者一连接就先看到上一代的残帧（实测如此）。
    """
    mgr = _mk_manager()
    sess = _mk_session(mgr)
    cursor = mgr.log.cursor()
    mgr.stop_session(requester=None, reason="stop")
    # 绕过 VideoSession.publish_frame 的 running 检查，直接模拟竞态窗口里的迟到发布
    mgr.log.publish_frame(sess.gen, 13, wsp.dumps_message({"type": "frame", "frame_id": 13}),
                          b"\xff\xd8JPEG")

    assert mgr.log.latest is None, "迟到帧不得复活最新帧"
    assert [e for e in _drain_cursor(cursor) if isinstance(e, FrameEvent)] == []
    # 停止之后才连上的观看者同样不该先收到上一代的残帧
    assert [e for e in _drain_cursor(mgr.log.cursor()) if isinstance(e, FrameEvent)] == []


def test_previous_generation_events_are_dropped_after_new_gen():
    """换代后旧代的迟到帧/控制事件一律不投递（否则会清掉新会话的画面与事件流）。"""
    mgr = _mk_manager()
    old = _mk_session(mgr)
    cursor = mgr.log.cursor()
    old_gen = old.gen
    new_gen = mgr.log.new_gen()

    _publish_frames(mgr, old_gen, (99,))
    mgr.log.publish_control(old_gen, ["stale"], clear=True)
    _publish_frames(mgr, new_gen, (1, 2))

    evs = _drain_cursor(cursor)
    assert [e.frame_id for e in evs if isinstance(e, FrameEvent)] == [1, 2], evs
    assert [e for e in evs if isinstance(e, ControlEvent)] == []
    assert mgr.log.latest is not None and mgr.log.latest.frame_id == 2


def test_new_cursor_after_stop_does_not_replay_retained_frames():
    """停止后新读者从队尾开始：帧环里残留的上一代帧不得重放。

    真实触发路径：edge 在某会话停止后重启/重连，上行流新建游标 —— 若从帧环头
    重放，hub.latest 会拿到一张上一代的定格画面，下一位观看者一连接就先看到它。
    """
    mgr = _mk_manager()
    sess = _mk_session(mgr)
    _publish_frames(mgr, sess.gen, (1, 2, 3))
    mgr.stop_session(requester=None, reason="stop")   # clear：_latest 置空，帧环仍有 1..3

    cursor = mgr.log.cursor()
    assert [e for e in _drain_cursor(cursor) if isinstance(e, FrameEvent)] == []

    gen = mgr.log.new_gen()
    _publish_frames(mgr, gen, (1,))
    evs = [e.frame_id for e in _drain_cursor(cursor) if isinstance(e, FrameEvent)]
    assert evs == [1], evs


# ==================== 观看者与停止策略 ====================

def test_last_viewer_leaving_stops_session():
    """最后一个观看者离开才停（多人下是对迁移前"任一断开即全局停"的有意改进）。"""
    mgr = _mk_manager()
    sess = _mk_session(mgr)
    a = mgr.attach_viewer("legacy", "alice")
    b = mgr.attach_viewer("legacy", "bob")

    mgr.detach_viewer(a.id)
    assert sess.running is True, "还有观看者的时候不该停"
    assert b.id in mgr.viewers

    mgr.detach_viewer(b.id)
    assert sess.running is False and sess.ended is True
    assert mgr.viewers == {}


def test_detach_releases_ownership_without_stopping_others():
    """非最后一位离开：只释放本机摄像头归属，会话照跑。"""
    mgr = _mk_manager()
    sess = _mk_session(mgr, kind="client")
    a = mgr.attach_viewer("legacy", "alice")
    b = mgr.attach_viewer("legacy", "bob")
    mgr.owner_viewer_id, mgr.owner_username = a.id, "alice"

    mgr.detach_viewer(b.id)
    assert mgr.owner_viewer_id == a.id and sess.running is True

    mgr.detach_viewer(a.id)
    assert mgr.owner_viewer_id is None and mgr.owner_username is None


def test_attach_viewer_is_idempotent_and_syncs_session_state():
    """登记可重复调用（Go 侧每个动作都会带 viewer id 回来），已有观看者身份不变。"""
    mgr = _mk_manager()
    a = mgr.attach_viewer("edge", "alice", viewer_id="v7")
    again = mgr.attach_viewer("edge", "alice", viewer_id="v7")
    assert again is a and len(mgr.viewers) == 1
    # 后来补充的用户名要能补上（首次登记可能还没解析出登录态）
    b = mgr.attach_viewer("edge", "carol", viewer_id="v8")
    assert b.username == "carol"
    assert mgr.attach_viewer("edge", None, viewer_id="v8").username == "carol"


def test_viewer_sync_reports_running_only_when_frames_exist():
    """连接同步：会话在跑且已有帧 → "视频源已开始输出画面"（源描述符用**会话**的）。"""
    mgr = _mk_manager()
    v = mgr.attach_viewer("edge", "alice", viewer_id="v1")
    assert mgr.viewer_sync(v) == {"gen": 0, "replies": []}, "没有会话时什么都不发"

    sess = _mk_session(mgr, echo={"kind": "rtsp", "url": "rtsp://192.168.10.20/s"})
    assert mgr.viewer_sync(v)["replies"] == [], "会话在跑但还没出帧（打开中）不发问候"

    _publish_frames(mgr, sess.gen, (5,))
    sync = mgr.viewer_sync(v)
    assert sync["gen"] == sess.gen
    assert _msgs(sync["replies"]) == [wsp.source_status_message(
        "running", sess.echo, message="视频源已开始输出画面", frame_id=5)]
    # 调用方给了"即将下发的帧号"就用它（Go 侧先发帧后同步时用这个）
    assert _msgs(mgr.viewer_sync(v, 99)["replies"])[0]["frame_id"] == 99


# ==================== 本机摄像头归属 ====================

def test_second_viewer_is_refused_and_running_session_untouched():
    """第二个观看者要本机摄像头：只回拒绝文案，**不动**正在跑的会话（仲裁在停旧会话之前）。"""
    mgr = _mk_manager()
    sess = _mk_session(mgr, kind="client")
    a = mgr.attach_viewer("legacy", "alice")
    b = mgr.attach_viewer("legacy", "bob")
    mgr.owner_viewer_id, mgr.owner_username = a.id, "alice"

    assert mgr.client_camera_available_for(b) is False
    out = mgr.handle_action(b, {"action": "start_client_camera"})
    # 兼容垫片先发一条 opening（老入口一律如此），随后才是拒绝文案
    assert _msgs(out.replies) == [
        {"type": "source_status", "state": "opening", "source": {"kind": "client"},
         "deprecated": True, "message": "该动作为兼容旧客户端保留，建议改用 open_source"},
        {"type": "status", "status": "error", "message": wsp.CLIENT_CAMERA_BUSY_MESSAGE},
    ], out.replies
    assert wsp.CLIENT_CAMERA_BUSY_MESSAGE == _fixture()["messages"]["client_camera_busy"]
    assert sess.running is True and mgr.session is sess, "被拒的请求不该影响正在跑的会话"
    assert mgr.owner_viewer_id == a.id, "归属不该因一次被拒的请求而改变"


def test_ghost_owner_is_reclaimed():
    """归属连接已失效（标签页卡死/断网）时回收归属，别人不再被永远挡住。"""
    mgr = _mk_manager()
    a = mgr.attach_viewer("legacy", "alice", conn=_Conn(WebSocketState.DISCONNECTED))
    b = mgr.attach_viewer("legacy", "bob", conn=_Conn(WebSocketState.CONNECTED))
    mgr.owner_viewer_id, mgr.owner_username = a.id, "alice"

    assert mgr.client_camera_available_for(b) is True
    assert mgr._claim_client_camera(b) is True
    assert mgr.owner_viewer_id == b.id and mgr.owner_username == "bob"
    # 同一个观看者重复占用：直接成功
    assert mgr._claim_client_camera(b) is True


def test_ws_alive_states():
    """`ws_alive` 的判据只认 CONNECTED；无 client_state 的对象（含 Go 观看者）按"非活"处理。"""
    assert vs.ws_alive(None) is False
    assert vs.ws_alive(_Conn(WebSocketState.CONNECTED)) is True
    assert vs.ws_alive(_Conn(WebSocketState.DISCONNECTED)) is False
    assert vs.ws_alive(object()) is False


def test_ownership_released_when_session_ends_without_first_frame():
    """会话没出过一帧就终止：释放归属（否则发起人再也点不动本机摄像头）。"""
    mgr = _mk_manager()
    sess = _mk_session(mgr, kind="client", requester="vA")
    mgr.owner_viewer_id, mgr.owner_username = "vA", "alice"

    mgr.stop_session(requester=None, reason="open_failed")
    assert mgr.owner_viewer_id is None and mgr.owner_username is None

    # 出过帧的会话终止：归属留给"主动停止"去释放（观看者还在，画面可能还要看）
    sess2 = _mk_session(mgr, kind="client", requester="vA")
    sess2.first_frame_seen = True
    mgr.owner_viewer_id, mgr.owner_username = "vA", "alice"
    mgr.stop_session(requester=None, reason="finished")
    assert mgr.owner_viewer_id == "vA"


def test_client_frame_rejects_non_owner():
    """只吃归属者推的帧：别人的画面混进来会被当成归属者自己的画面上屏（实测踩过）。"""
    mgr = _mk_manager()
    sess = _mk_session(mgr, kind="client")
    mgr.owner_username = "alice"

    mgr.inject_client_frame("bob", "AAAA")
    assert list(sess.client_frames) == [], "非归属者的帧不得入队"

    # 归属未知（还没人占用）时不拦：与迁移前一致（登记与推帧之间本就有时间差）
    mgr.owner_username = None
    mgr.inject_client_frame_bytes("bob", b"not-a-jpeg")
    assert list(sess.client_frames) == [], "解不开的字节不该入队"


# ==================== 动作回执（ActionOutcome） ====================

def test_action_outcome_json_shape():
    """回执形状是跨语言契约（Go 侧按这五个键解析）。"""
    out = ActionOutcome(replies=["a", "b"], framed_tail="t", clear=True, gen=3, replaced=True)
    assert out.to_json() == {"gen": 3, "replaced": True, "clear": True,
                             "replies": ["a", "b"], "framed_tail": "t"}


def test_ping_and_unknown_actions():
    mgr = _mk_manager()
    v = mgr.attach_viewer("edge", "alice")
    pong = _fixture()["messages"]["pong_int"]
    assert mgr.handle_action(v, {"action": "ping", "ts": 0}).replies == [pong]
    assert mgr.handle_action(v, {"type": "ping", "ts": 0}).replies == [pong], "前端心跳是 {type:ping}"
    assert mgr.handle_action(v, {"action": "没这个动作"}).replies == []
    assert mgr.handle_action(v, {}).gen == mgr.log.gen


def test_pause_resume_toggles_session():
    mgr = _mk_manager()
    v = mgr.attach_viewer("edge", "alice")
    sess = _mk_session(mgr)
    mgr.handle_action(v, {"action": "pause"})
    assert sess.paused is True
    mgr.handle_action(v, {"action": "resume"})
    assert sess.paused is False
    # 没有会话时是空操作，不该炸（前端在别的标签页停过）
    mgr.session = None
    assert mgr.handle_action(v, {"action": "pause"}).replies == []


def test_stop_action_reply_messages_and_order():
    """停止回执：m1（应用态，带 segment_analysis）+ m2（协议态 source_status），顺序冻结。"""
    mgr = _mk_manager()
    v = mgr.attach_viewer("edge", "alice", viewer_id="vA")
    _mk_session(mgr, echo={"kind": "client"})

    out = mgr.handle_action(v, {"action": "stop"})
    assert _msgs(out.replies) == [
        {"type": "status", "status": "stopped",
         "message": "视频已停止，WebSocket 保持连接", "segment_analysis": None},
        wsp.source_status_message("stopped", {"kind": "client"}, message="视频已停止"),
    ]
    assert out.clear is True
    assert out.framed_tail == _fixture()["messages"]["stream_stopped_tail"], \
        "有会话就有收尾消息（观看者自己按「是否收到过帧」决定发不发）"
    # 已经停过再点一次：同样要回执（前端靠它复位按钮），但不再有收尾消息
    again = mgr.handle_action(v, {"action": "stop"})
    assert again.replies == out.replies and again.framed_tail is None


def test_stop_action_keeps_tail_for_requester_with_frames():
    """发起人已收到过帧时，收尾消息走回执的 framed_tail（而不是控制事件）。"""
    mgr = _mk_manager()
    v = mgr.attach_viewer("edge", "alice", viewer_id="vA")
    cursor = v.cursor
    _mk_session(mgr, echo={})
    _publish_frames(mgr, mgr.log.gen, (1,))
    _drain_cursor(cursor)

    out = mgr.handle_action(v, {"action": "stop"})
    assert out.framed_tail == _fixture()["messages"]["stream_stopped_tail"]
    ev = [e for e in _drain_cursor(cursor) if isinstance(e, ControlEvent)][0]
    assert ev.except_viewer == "vA", "发起人不该再收一遍控制事件"


def test_open_source_rejection_reports_source_status():
    """来源被守卫拒绝：回 source_status(error) + 具体 code，且**不建会话**。"""
    mgr = _mk_manager()
    v = mgr.attach_viewer("edge", "alice")
    out = mgr.handle_action(v, {"action": "open_source",
                                "source": {"kind": "rtsp", "url": "rtsp://8.8.8.8/x"}})
    payload = _msgs(out.replies)[0]
    assert payload["type"] == "source_status" and payload["state"] == "error"
    assert payload["source"] == {"kind": "rtsp", "url": "rtsp://8.8.8.8/x"}, "原样回显输入"
    assert isinstance(payload.get("code"), str) and payload["code"]
    assert mgr.session is None, "被拒的来源不该建会话"
    # 畸形输入同样只是被拒（"kind 不认识"），不影响进程
    out2 = mgr.handle_action(v, {"action": "open_source", "source": {"kind": "nope"}})
    assert _msgs(out2.replies)[0]["state"] == "error"


def test_legacy_start_file_still_guarded():
    """老入口 start_file 的路径守卫**没有**因迁移而削弱（非 open_source 翻译来的才校验）。"""
    mgr = _mk_manager()
    v = mgr.attach_viewer("legacy", "alice")
    out = mgr.handle_action(v, {"action": "start_file", "file_path": "../secret.mp4"})
    msgs = _msgs(out.replies)
    # 三条：兼容垫片（opening/deprecated）+ 应用态错误 + 协议态 source_status（顺序不变）
    assert [m["type"] for m in msgs] == ["source_status", "status", "source_status"], msgs
    assert msgs[0]["state"] == "opening" and msgs[0]["deprecated"] is True
    assert msgs[1]["status"] == "error" and "视频源不被允许" in msgs[1]["message"]
    assert msgs[2]["state"] == "error" and msgs[2]["code"]
    assert mgr.session is None


def test_start_replaces_previous_session_and_clears_requester():
    """开新会话：旧会话停掉（其余观看者收控制事件），发起人自己拿到 clear + replaced。"""
    mgr = _mk_manager()
    old = _mk_session(mgr, kind="device")
    a = mgr.attach_viewer("edge", "alice", viewer_id="vA")
    b = mgr.attach_viewer("edge", "bob", viewer_id="vB")
    _publish_frames(mgr, old.gen, (1,))
    _drain_cursor(a.cursor)
    _drain_cursor(b.cursor)

    started = []
    mgr.start_session = lambda *args: started.append(args)     # 不加载模型
    out = mgr.handle_action(a, {"action": "start_webcam", "camera_id": 0})

    assert started and old.running is False
    assert out.replaced is True and out.clear is True and out.framed_tail is None
    assert _msgs(out.replies)[0]["state"] == "opening" and _msgs(out.replies)[0]["deprecated"] is True
    # 停止事件只投递一份，带 except_viewer=发起人 —— **读的人**据此跳过
    # （发起人自己的复位走回执的 clear，不收"视频流已停止"）
    evs_a = [e for e in _drain_cursor(a.cursor) if isinstance(e, ControlEvent)]
    evs_b = [e for e in _drain_cursor(b.cursor) if isinstance(e, ControlEvent)]
    assert len(evs_a) == len(evs_b) == 1, (evs_a, evs_b)
    assert evs_b[0] is evs_a[0], "同一条事件被判给两个游标（各读各的，判据在读者侧）"
    assert evs_a[0].except_viewer == "vA"
    assert evs_b[0].framed_tail == _fixture()["messages"]["stream_stopped_tail"]


def test_start_claims_client_camera_for_requester():
    mgr = _mk_manager()
    v = mgr.attach_viewer("edge", "alice", viewer_id="vA")
    mgr.start_session = lambda *args: None
    out = mgr.handle_action(v, {"action": "start_client_camera"})
    assert mgr.owner_viewer_id == "vA" and mgr.owner_username == "alice"
    assert out.clear is True and out.replaced is False


def test_start_failure_reports_error_and_releases_ownership():
    """建会话抛异常：回一条 error（文案带原因），并释放刚占上的本机摄像头归属。"""
    mgr = _mk_manager()
    v = mgr.attach_viewer("edge", "alice", viewer_id="vA")

    def boom(*args):
        raise RuntimeError("模型没加载")

    mgr.start_session = boom
    out = mgr.handle_action(v, {"action": "start_client_camera"})
    assert _msgs(out.replies)[-1]["message"] == "视频启动失败: 模型没加载"
    assert mgr.owner_viewer_id is None


# ==================== 看门狗 ====================

def test_open_timeout_terminates_and_notifies_all():
    """打开超时：到点还没出过一帧就终止，并把原因确定投递给所有观看者。"""
    mgr = _mk_manager()
    v = mgr.attach_viewer("edge", "alice", viewer_id="vA")
    sess = _mk_session(mgr, kind="rtsp")
    _drain_cursor(v.cursor)
    sess.pipeline_started_at = time.time() - (VIDEO_OPEN_TIMEOUT_SECONDS + 1)

    mgr._tick_monitor()
    assert sess.running is False and sess.terminal_kind == "error"
    evs = [e for e in _drain_cursor(v.cursor) if isinstance(e, ControlEvent)]
    assert len(evs) == 1
    payload = _msgs(evs[0].texts)[0]
    assert payload["type"] == "source_status" and payload["state"] == "error"
    assert payload["code"] == "open_timeout"
    assert evs[0].framed_tail == _fixture()["messages"]["stream_stopped_tail"]
    assert evs[0].clear is True

    # 没到点、或已经出过帧：都不动它
    sess2 = _mk_session(mgr, kind="rtsp")
    sess2.pipeline_started_at = time.time()
    mgr._tick_monitor()
    assert sess2.running is True
    sess3 = _mk_session(mgr, kind="rtsp")
    sess3.pipeline_started_at = time.time() - (VIDEO_OPEN_TIMEOUT_SECONDS + 1)
    sess3.first_frame_seen = True
    mgr._tick_monitor()
    assert sess3.running is True


def test_edge_stream_grace_reclaims_viewers():
    """Go 上行流断开超过宽限期：它登记过的观看者视为失联并回收（重连后重新登记）。"""
    mgr = _mk_manager()
    sess = _mk_session(mgr)
    keep = mgr.attach_viewer("legacy", "alice", viewer_id="vL")
    gone = mgr.attach_viewer("edge", "bob", viewer_id="vE")

    mgr.edge_stream_opened()
    mgr.edge_stream_closed()
    assert mgr._edge_lost_at is not None
    mgr._tick_monitor()
    assert "vE" in mgr.viewers, "宽限期内不动它（Go 重启/瞬断）"

    mgr._edge_lost_at = time.time() - (vs._EDGE_GRACE_SECONDS + 1)
    mgr._tick_monitor()
    assert "vE" not in mgr.viewers and "vL" in mgr.viewers
    assert sess.running is True, "还有观看者，会话照跑"


def test_session_status_defaults_and_after_stop():
    """状态端点口径：无会话时的默认值与迁移前一致（source 恒为 null）。"""
    mgr = _mk_manager()
    assert mgr.get_stream_status() == {
        "running": False, "thread_alive": False, "source": None, "paused": False,
        "current_frame": 0, "target_fps": float(vs.VIDEO_FPS), "mode": "retail"}
    assert mgr.get_emotion_camera_status() == {"running": False, "start_time": 0.0}

    sess = _mk_session(mgr)
    mgr.stop_session(requester=None, reason="stop")
    st = mgr.get_stream_status()
    assert st["running"] is False and st["source"] is None and st["mode"] == "retail"
    assert st["current_frame"] == sess.current_frame == 0


# ==================== 上行信封（跨语言 golden） ====================

def test_envelope_matches_fixture():
    """上行信封逐字节比对契约夹具（Go 侧解析器按同一份写）。"""
    fx = _fixture()["upstream_envelope"]
    assert fx["magic"] == vi._MAGIC.decode("ascii")
    assert fx["version"] == vi._VERSION
    assert fx["kinds"] == {"frame": vi._KIND_FRAME, "control": vi._KIND_CONTROL,
                           "heartbeat": vi._KIND_HEARTBEAT}
    for ex in fx["examples"]:
        got = vi._envelope(ex["kind"], ex["frame_id"],
                           ex["meta_utf8"].encode("utf-8"),
                           bytes.fromhex(ex["jpeg_hex"]))
        assert got.hex() == ex["envelope_hex"], ex["name"]
    # 心跳信封定长：18 字节头 + 空 meta/jpeg
    assert len(vi._envelope(vi._KIND_HEARTBEAT, 0, b"", b"")) == 18


def test_stream_chunks_emits_frames_controls_and_heartbeats():
    """上行流：帧/控制事件按序成信封推出，闲着发心跳；生成器收尾时注销在场登记。"""
    mgr = _mk_manager()
    old_manager, old_hb = vi.MANAGER, vi._HEARTBEAT_SECONDS
    vi.MANAGER, vi._HEARTBEAT_SECONDS = mgr, 0.05
    stream = vi._stream_chunks()
    try:
        gen = mgr.log.new_gen()
        mgr.log.publish_frame(gen, 42, wsp.dumps_message({"type": "frame", "frame_id": 42}),
                              b"\xff\xd8JPEG")
        # 生成器是惰性的：第一次 next() 才真正开始订阅（登记在场 + 造游标）
        frame_env = _parse_envelope(next(stream))
        assert mgr._edge_streams == 1, "订阅即登记在场（Go 断线宽限靠它判定）"
        assert frame_env["kind"] == vi._KIND_FRAME and frame_env["frame_id"] == 42
        meta = json.loads(frame_env["meta_utf8"])
        assert meta["gen"] == gen and meta["frame_id"] == 42
        assert json.loads(meta["msg"])["frame_id"] == 42, "msg 是原样的帧元数据 WS 文本"
        assert frame_env["jpeg"] == b"\xff\xd8JPEG"

        mgr.log.publish_control(gen, ["T1", "T2"], framed_tail="TAIL", clear=True,
                                except_viewer="vX")
        ctl = _parse_envelope(next(stream))
        assert ctl["kind"] == vi._KIND_CONTROL and ctl["frame_id"] == 0 and ctl["jpeg"] == b""
        assert json.loads(ctl["meta_utf8"]) == {"gen": gen, "clear": True, "except": "vX",
                                                "framed_tail": "TAIL", "texts": ["T1", "T2"]}

        hb = _parse_envelope(next(stream))          # 没有新事件 → 心跳
        assert hb["kind"] == vi._KIND_HEARTBEAT and hb["meta_utf8"] == "" and hb["jpeg"] == b""
    finally:
        stream.close()                              # 必须在还原 MANAGER 之前收尾
        vi.MANAGER, vi._HEARTBEAT_SECONDS = old_manager, old_hb
    assert mgr._edge_streams == 0 and mgr._edge_lost_at is not None


def test_internal_vision_endpoints_wiring():
    """内部端点的接线：登记/注销/动作转发（Go 侧调用的就是这三个形状）。"""
    mgr = _mk_manager()
    old_manager = vi.MANAGER
    vi.MANAGER = mgr
    try:
        assert vi.vision_viewer(vi.ViewerRequest(op="attach", viewer="v9",
                                                 username="alice")) == \
            {"viewer": "v9", "gen": 0, "replies": []}

        act = vi.vision_action(vi.ActionRequest(viewer="v9", username="alice",
                                                message={"action": "ping", "ts": 0}))
        assert act["replies"] == [_fixture()["messages"]["pong_int"]]
        assert act["gen"] == 0 and act["clear"] is False and act["replaced"] is False
        assert act["framed_tail"] is None

        # 会话在跑且有帧时，登记会带回"已在运行"问候
        sess = _mk_session(mgr)
        _publish_frames(mgr, sess.gen, (3,))
        out = vi.vision_viewer(vi.ViewerRequest(op="attach", viewer="v10", frame_id=3))
        assert _msgs(out["replies"]) == [wsp.source_status_message(
            "running", sess.echo, message="视频源已开始输出画面", frame_id=3)]

        assert vi.vision_viewer(vi.ViewerRequest(op="detach", viewer="v9"))["replies"] == []
        assert "v9" not in mgr.viewers

        resp = vi.vision_client_frame(b"\xff\xd8", username="alice")
        assert resp.status_code == 204
    finally:
        vi.MANAGER = old_manager


def test_adapter_drain_keeps_controls_and_only_latest_frame():
    """传统适配器的排空规则：控制事件全保留且保持相对顺序，帧只发最新一帧。"""
    mgr = _mk_manager()
    gen = mgr.log.new_gen()
    cursor = mgr.log.cursor()
    _publish_frames(mgr, gen, (1, 2))
    mgr.log.publish_control(gen, ["M"])
    _publish_frames(mgr, gen, (3, 4))

    events = _drain(cursor)
    assert [type(e).__name__ for e in events] == ["ControlEvent", "FrameEvent"]
    assert events[0].texts == ["M"] and events[1].frame_id == 4
    assert _drain(cursor) == [], "取空之后没有新事件"
