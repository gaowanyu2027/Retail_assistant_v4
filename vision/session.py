"""视频会话：会话状态、观看者注册表与事件日志。

背景：迁移前 `api/routes/stream.py` 用一组**模块级全局**（`_active` / `_run_token` /
`_latest_result` / `_latest_frame_jpeg` / `_client_frames` / `_client_camera_owner`）表示
"当前视频会话"，每条 WS 连接用 200Hz 轮询这些全局来推帧。视频数据面迁 Go（S3）后，
会话状态必须是**跨连接共享、可被多条读者独立消费**的东西，于是收进对象：

- `VideoSession`：当前会话（运行/暂停/帧号/最新帧/客户端摄像头帧队列/运行令牌）；
- `Viewer`：一个观看者（一条 `/ws/stream` 连接，Go 边缘与 Python 适配器都会登记），
  带**独立事件游标**——慢观看者自动跳到最新帧，不会拖慢别人；
- `EventLog`：会话事件日志（帧环 + 控制事件），进程内唯一发布点；
- `SessionManager`：全局唯一，负责会话生命周期、观看者注册表、客户端摄像头归属仲裁、
  「打开超时」看门狗。

约定（与 `ws_protocol.py` 的分工）：**所有下发的文本消息都在本模块组装**，
读者（Go 边缘 / Python 适配器）只做转发或二进制封装；这样消息格式只有一处定义，
Go 侧不必复刻任何业务文案（含中文字节与 JSON 键序）。

与迁移前的**有意差异**（逐条写进改进记录，此处给读者一个索引）：

1. 最后一个观看者离开**才**停会话（迁移前：任一观看者断开即停全局会话）；
2. 终止类消息（停止 / 播放完毕 / 异常终止 / 打开失败）**确定投递**给所有观看者
   （迁移前：谁先轮询到谁收到）；
3. 新连接不再清空共享的"最新帧"（迁移前每个新观看者都会清一次，导致在场其他人
   收到一次"视频流已停止"，画面闪断）；
4. 停止之后不再补发"迟到帧"（迁移前处理线程可能在停止命令之后又推出一帧，
   观看者会看到"已停止"之后又冒出画面）；
5. 打开源失败不再连坐进行中的会话（迁移前会先把旧源 release 掉，旧会话以
   "视频播放完毕"收场）；打开动作本身也移到处理线程，源不可达时不再冻住
   这条连接的心跳与收发。
"""
from __future__ import annotations

import base64
import threading
import time as _time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# ⚠ cv2 / numpy **不**在模块级导入（与 agents / cv_engine / skills 的包入口同一手法）：
# 本模块承载的会话语义（事件日志、丢帧、观看者仲裁、停止策略）都是纯逻辑，
# 必须能在 CI 的最小依赖环境里被测试；重依赖只在真正用到像素的两个函数里延迟导入。
from config.settings import (
    CLIENT_FRAME_QUEUE_MAXLEN,
    LOCAL_SAMPLE_FRAMES,
    VIDEO_CAMERA_HEIGHT,
    VIDEO_CAMERA_WIDTH,
    VIDEO_FPS,
    VIDEO_OPEN_TIMEOUT_SECONDS,
)
from ws_protocol import (
    CLIENT_CAMERA_BUSY_MESSAGE,
    dumps_message,
    pong_message,
    source_status_message,
)

# ==================== 事件日志 ====================

# 帧事件环形容量：够慢观看者追上，又不会积压 —— 读者落后超过这个量时，更旧的帧
# 已被环挤掉（积压上限即环容量；读者侧另有一层丢旧留新，见 EventCursor._pick）
_FRAME_RING = 4
# 控制事件环形容量：停止/错误这类消息不能丢，但也没有无限保留的必要
_CONTROL_RING = 512

# Go 边缘上行流断开后的宽限期：这段时间内重连视为同一次会话的读者（Go 重启/瞬断），
# 超过则判定它登记过的观看者都已失联（见 SessionManager._monitor_loop）
_EDGE_GRACE_SECONDS = 10.0

# 服务关闭/主动停止时，处理线程的等待上限（迁移前分别为 1.0s / 2.0s，统一取大者）
_THREAD_JOIN_TIMEOUT = 2.0


def ws_alive(ws) -> bool:
    """这条 WS 是否还处于连接状态（用于回收"已断开的归属者"）。

    ⚠ 为什么需要：本机摄像头的归属是**独占**标记，正常只在连接关闭时释放。
    但如果那条连接以非正常方式失效（标签页卡死、网络断、异常路径），标记就可能
    一直留着 —— 之后任何人点「本机摄像头」都只会收到"已有其他用户正在使用"，
    而界面上看不出该找谁释放（实测踩过：一次失败的尝试把模式占住了）。
    这里在占用检查前先探一次连接状态，断开的直接回收。
    """
    try:
        from starlette.websockets import WebSocketState
        return ws is not None and getattr(ws, "client_state", None) == WebSocketState.CONNECTED
    except Exception:
        return ws is not None


@dataclass
class FrameEvent:
    """一帧：二进制包由读者组装（Go 侧 `[4B frame_id][JPEG]`，与 ws_protocol 一致）。"""
    seq: int
    gen: int
    frame_id: int
    meta_text: str          # 帧元数据消息的 JSON 文本（是否下发由读者按节拍决定）
    jpeg: bytes


@dataclass
class ControlEvent:
    """一条或多条控制消息 + 收尾语义。

    - `texts`：逐字转发给客户端的 WS 文本（已是完整 JSON 文本）；
    - `framed_tail`：**仅发给"已经收到过帧"的观看者**的收尾消息（"视频流已停止"）。
      迁移前这条消息由每条连接自己判断要不要发（`_last_pushed_frame_id >= 0`），
      这里保留同一判据，但判据由读者在**复位之前**求值；
    - `clear`：此前推送的帧全部作废（读到此事件后，读者应丢弃待发帧并复位"已发帧号"）；
    - `except_viewer`：不发给这个观看者（发起人自己走 HTTP 回执，避免重复）。
    """
    seq: int
    gen: int
    texts: list[str] = field(default_factory=list)
    framed_tail: str | None = None
    clear: bool = False
    except_viewer: str | None = None


class EventLog:
    """会话事件的唯一发布点；每个读者一个游标，互不影响。

    发布者（处理线程 / 会话生命周期）永不阻塞：帧只保留最近 `_FRAME_RING` 条，
    读者跟不上就跳帧；控制事件保留最近 `_CONTROL_RING` 条。
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._seq = 0
        self._gen = 0
        # 最近一次"停止/作废"（clear）的代际；该代之后的迟到帧一律丢弃。
        self._sealed_gen = -1
        self._frames: deque[FrameEvent] = deque(maxlen=_FRAME_RING)
        self._controls: deque[ControlEvent] = deque(maxlen=_CONTROL_RING)
        self._latest: FrameEvent | None = None

    # ---- 发布 ----
    def new_gen(self) -> int:
        """开新一代（新会话）：帧号从 1 重新计数，读者靠 gen 变化复位。"""
        with self._cv:
            self._gen += 1
            self._sealed_gen = -1
            self._frames.clear()
            self._latest = None
            self._cv.notify_all()
            return self._gen

    def publish_frame(self, gen: int, frame_id: int, meta_text: str, jpeg: bytes) -> None:
        with self._cv:
            # 迟到帧丢弃：处理线程的 running 检查与这里的发布不是原子的，停止命令
            # 可能恰好落在两者之间。若放进来，它会排在 clear 事件之后把 _latest
            # 复活，新观看者连接时会先看到上一代的残帧（实测：重启 edge 后首个
            # 观看者收到上一会话的最后一帧，帧号从 13 跳到 1）。
            if gen != self._gen or gen == self._sealed_gen:
                return
            self._seq += 1
            ev = FrameEvent(self._seq, gen, frame_id, meta_text, jpeg)
            self._frames.append(ev)
            self._latest = ev
            self._cv.notify_all()

    def publish_control(self, gen: int, texts: list[str], *,
                        framed_tail: str | None = None, clear: bool = False,
                        except_viewer: str | None = None) -> None:
        if not texts and framed_tail is None and not clear:
            return
        with self._cv:
            # 换代之后旧代的控制事件也不再投递（否则旧会话迟到的"已停止"会把
            # 新会话刚发布的 _latest 清掉）。
            if gen != self._gen:
                return
            self._seq += 1
            self._controls.append(ControlEvent(
                self._seq, gen, list(texts), framed_tail, clear, except_viewer))
            if clear:
                self._sealed_gen = gen
                self._latest = None
            self._cv.notify_all()

    # ---- 读取 ----
    def cursor(self) -> "EventCursor":
        """新读者的游标：从"当前最新帧"开始（新观看者要立刻看到画面）。

        起点定在最新帧的**前一条**：这样最新帧会被投递一次，更早的帧不重放；
        历史控制事件一概不补发（它们是发给当时在场观众的）。当前**没有**最新帧
        （未开播 / 刚停止，`_latest` 已被 clear 置空）时从队尾开始：帧环里可能还留着
        上一代的帧，重放它们会让新读者先看到一张定格旧画面（edge 重启后实测）。
        """
        with self._cv:
            return EventCursor(self)

    @property
    def gen(self) -> int:
        with self._cv:
            return self._gen

    @property
    def latest(self) -> FrameEvent | None:
        with self._cv:
            return self._latest


class EventCursor:
    """单个读者的事件游标（每个读者独占，不跨线程使用）。

    `next()` 按 seq 顺序返回帧/控制事件；没有新事件时按 `timeout` 等待。
    """

    def __init__(self, log: EventLog) -> None:
        self._log = log
        with log._cv:
            # 起点=最新帧的前一条：最新帧投递一次，更早的帧不重放。
            # 没有最新帧（未开播/已停止）时从**当前队尾**开始：帧环里残留的
            # 上一代帧不得重放给新读者。
            self._frame_seq = (log._latest.seq - 1) if log._latest else log._seq
            self._ctrl_seq = log._seq

    def _pick(self) -> FrameEvent | ControlEvent | None:
        """取下一个该发的事件（调用方持有日志锁）。

        帧环只留最近 `_FRAME_RING` 条，因此**积压上限就是这个环**：游标落后不超过
        环容量时按序补齐；落后更多时从环里最早的一条续起，环外的旧帧直接丢弃
        （`_latest` 必定在环里 —— 每次发布同时写环与 `_latest`，故无需"跳最新"的兜底）。
        真正意义上的"丢旧留新"发生在读者侧：Go 每客户端 outbound 容量 1，
        传统适配器 `_drain` 只保留最新一帧。
        """
        ctrl = next((e for e in self._log._controls if e.seq > self._ctrl_seq), None)
        frame = next((e for e in self._log._frames if e.seq > self._frame_seq), None)
        if ctrl is None:
            return frame
        if frame is None:
            return ctrl
        return ctrl if ctrl.seq < frame.seq else frame

    def next(self, timeout: float = 0.0):
        deadline = _time.monotonic() + timeout
        with self._log._cv:
            while True:
                ev = self._pick()
                if ev is not None:
                    if isinstance(ev, FrameEvent):
                        self._frame_seq = ev.seq
                    else:
                        self._ctrl_seq = ev.seq
                    return ev
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    return None
                self._log._cv.wait(remaining)


# ==================== 观看者 ====================

class Viewer:
    """一个观看者 = 一条 `/ws/stream` 连接（Go 边缘或传统适配器持有）。

    `id` 由会话管理发号（Go 侧通过 /internal/vision/viewer 领取并回传）；
    `conn` 只在传统适配器里有值（用于回收"已断开的归属者"）。
    """

    def __init__(self, viewer_id: str, kind: str, username: str | None,
                 conn: Any = None) -> None:
        self.id = viewer_id
        self.kind = kind                 # "edge" | "legacy"
        self.username = username
        self.conn = conn
        self.connected_at = _time.time()
        self.cursor: EventCursor = None  # 由 attach_viewer 注入（需要 manager.log）

    def __repr__(self) -> str:  # 日志用
        return f"<Viewer {self.id} {self.kind} {self.username!r}>"


# ==================== 会话 ====================

class VideoSession:
    """一次视频分析会话：一个源、一个处理线程、一个代际号 gen。"""

    def __init__(self, manager: "SessionManager", sid: str, gen: int, mode: str,
                 kind: str, params: dict, echo: dict, requester: str | None) -> None:
        self.manager = manager
        self.id = sid
        self.gen = gen
        self.mode = mode                     # "retail" | "emotion"
        self.kind = kind                     # camera|device|file|upload|client|rtsp
        self.params = params
        self.echo = echo or {}
        self.requester = requester           # 发起会话的观看者 id（控制事件对他除外）

        self.running = False
        self.paused = False
        # 源标识：与迁移前保持同一取值（旧实现里 _active["source"] 在 start_processing
        # 开头就被复位成 None，状态端点因此始终返回 null；这里不顺手改口径）
        self.source = None
        self.camera_id = 0
        self.current_frame = 0
        self.target_fps = float(VIDEO_FPS)
        self.run_token = object()

        # 打开/首帧：看门狗与"会话失败即释放归属"的依据
        self.pipeline_started_at: float | None = None
        self.first_frame_seen = False
        self.terminal_kind: str | None = None    # "error" | "finished"
        self.ended = False

        # 本机摄像头（kind=client）：浏览器推上来的待处理帧
        self.client_frames: deque = deque(maxlen=CLIENT_FRAME_QUEUE_MAXLEN)
        self.client_frame_lock = threading.Lock()

        # 表情模式专用状态（迁移前的 _emo_state）
        self.emo_running = False
        self.emo_start_time = 0.0
        self.emo_sample_buffer: deque = deque(maxlen=LOCAL_SAMPLE_FRAMES)
        self.emo_batch_records: list = []
        self.emo_camera_id = "camera_entrance"
        self.emo_lock = threading.Lock()

        self.cap = None                      # cv2.VideoCapture（处理线程持有）
        self.bg_thread: threading.Thread | None = None

        # 出帧用：最新一张 JPEG（非检测帧沿用上一张，与迁移前一致）
        self.latest_jpeg: bytes | None = None
        # 处理链组件（由 SessionManager 在建会话时注入）
        self.processor = None
        self.event_detector = None
        self.roi_manager = None
        self.face_emotion = None

    # ---- 状态 ----
    def is_running(self) -> bool:
        return self.running

    def release_capture(self) -> None:
        cap, self.cap = self.cap, None
        if cap is not None:
            try:
                import cv2                      # 延迟导入（见文件头说明）
                if isinstance(cap, cv2.VideoCapture):
                    cap.release()
            except Exception:
                pass

    def pop_client_frame(self):
        with self.client_frame_lock:
            if self.client_frames:
                return self.client_frames.popleft()
            return None

    def publish_frame(self, frame_id: int, meta: dict, jpeg: bytes) -> None:
        """处理线程出一帧：组装元数据文本并推入事件日志（各读者自行取用）。

        会话一旦停止/被替换就不再发布 —— 迁移前处理线程可能在停止命令之后又推出
        一帧（推理已在进行中），观看者会在"已停止"之后看到画面又冒出来。
        """
        if not self.running:
            return
        self.first_frame_seen = True
        self.manager.log.publish_frame(self.gen, int(frame_id),
                                       dumps_message(meta), jpeg)

    def __repr__(self) -> str:
        return f"<VideoSession {self.id} gen={self.gen} {self.mode} {self.kind}>"


@dataclass
class ActionOutcome:
    """一条动作消息的处理结果（回给**发起人**）。

    - `replies`：按序下发的文本消息（已是完整 JSON 文本）；
    - `framed_tail`：只发给"已经收到过帧"的发起人的收尾消息（视频流已停止）；
    - `clear`：发起人应丢弃待发帧并复位"已发帧号"（本动作停掉/替换了会话）。
    """
    replies: list[str] = field(default_factory=list)
    framed_tail: str | None = None
    clear: bool = False
    gen: int = 0
    replaced: bool = False

    def to_json(self) -> dict:
        return {
            "gen": self.gen,
            "replaced": self.replaced,
            "clear": self.clear,
            "replies": self.replies,
            "framed_tail": self.framed_tail,
        }


# ==================== 会话管理 ====================

class SessionManager:
    """全局唯一的会话管理器（进程内一份；Go 边缘与 Python 适配器共用）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.log = EventLog()
        self.viewers: dict[str, Viewer] = {}
        self.session: VideoSession | None = None   # 当前会话（停止后保留，供状态端点取值）
        self._sid = 0
        self._viewer_seq = 0

        # 本机摄像头归属（用户名判帧 + 连接判仲裁，与迁移前同一套语义）
        self.owner_viewer_id: str | None = None
        self.owner_username: str | None = None

        # 处理链组件（懒加载一次，迁移前是"每条连接各建一套"）
        self._components: dict[str, Any] = {}

        # Go 上行流（/internal/vision/events）的在场情况
        self._edge_streams = 0
        self._edge_lost_at: float | None = None
        self._edge_viewers: set[str] = set()

        self._shutdown = False
        self._monitor_started = False

    # ---------- 组件（懒加载） ----------

    def components(self) -> dict:
        """零售/表情管线组件；首次调用时构建（模型加载可能耗时，调用方在 worker 线程）。"""
        with self._lock:
            if self._components:
                return self._components
            from api.dependencies import (
                get_detector, get_roi_manager, get_popularity_skill,
                get_anomaly_skill, get_emotion_skill, get_face_emotion,
            )
            from api.events import VideoEventDetector
            from cv_engine.tracker import TrackStateManager
            from cv_engine.video_processor import VideoProcessor
            from config.settings import (
                FACE_EMOTION_INTERVAL, FRAME_SKIP, LOCAL_ROI_CONFIG_PATH, PROJECT_ROOT,
            )
            from cv_engine.roi_manager import ROIManager

            face_emotion = None
            try:
                face_emotion = get_face_emotion()
            except Exception as e:
                print(f"[WS] 表情分析模块加载失败: {e}")

            detector = get_detector()
            roi_mgr = get_roi_manager()
            track_mgr = TrackStateManager()
            local_roi_mgr = ROIManager(str(PROJECT_ROOT / LOCAL_ROI_CONFIG_PATH))
            self._components = {
                "detector": detector,
                "roi_mgr": roi_mgr,
                "local_roi_mgr": local_roi_mgr,
                "face_emotion": face_emotion,
                "pop_skill": get_popularity_skill(),
                "anom_skill": get_anomaly_skill(),
                "emo_skill": get_emotion_skill(),
                "processor": VideoProcessor(detector, track_mgr, roi_mgr,
                                            frame_skip=FRAME_SKIP, face_emotion=face_emotion,
                                            face_emotion_interval=FACE_EMOTION_INTERVAL),
                "local_processor": VideoProcessor(detector, track_mgr, local_roi_mgr,
                                                  frame_skip=FRAME_SKIP, face_emotion=face_emotion,
                                                  face_emotion_interval=FACE_EMOTION_INTERVAL),
                "event_detector": VideoEventDetector(roi_mgr),
                "local_event_detector": VideoEventDetector(local_roi_mgr),
            }
            return self._components

    # ---------- 观看者注册表 ----------

    def attach_viewer(self, kind: str, username: str | None, conn: Any = None,
                      viewer_id: str | None = None) -> Viewer:
        with self._lock:
            if viewer_id and viewer_id in self.viewers:
                v = self.viewers[viewer_id]
                v.conn = conn or v.conn
                v.username = username or v.username
                if kind == "edge":
                    self._edge_viewers.add(v.id)
                return v
            self._viewer_seq += 1
            vid = viewer_id or f"v{self._viewer_seq}"
            v = Viewer(vid, kind, username, conn)
            v.cursor = self.log.cursor()
            self.viewers[vid] = v
            if kind == "edge":
                self._edge_viewers.add(vid)
            return v

    def detach_viewer(self, viewer_id: str | None) -> None:
        """观看者离开：释放本机摄像头归属；**最后一个离开才停会话**。"""
        if viewer_id is None:
            return
        with self._lock:
            v = self.viewers.pop(viewer_id, None)
            if v is None:
                return
            self._edge_viewers.discard(viewer_id)
            if self.owner_viewer_id == viewer_id:
                self.owner_viewer_id = None
                self.owner_username = None
            last = not self.viewers
            sess = self.session
        if last and sess is not None and sess.running:
            print(f"[WS] 最后一个观看者已离开，停止会话 {sess.id}")
            self.stop_session(requester=None, reason="last_viewer_left")

    def viewer_sync(self, viewer: Viewer, frame_id: int | None = None) -> dict:
        """观看者连接/换代时的同步：告诉它"当前这一代在跑、帧号是多少"。

        返回 `{"gen": G, "replies": [...]}`；`replies` 是"视频源已开始输出画面"
        这类随连接下发的消息（源描述符用**会话**的，不是观看者自己的）。
        """
        with self._lock:
            sess = self.session
            gen = self.log.gen
            echo = sess.echo if sess else {}
            running = bool(sess and sess.running)
        replies: list[str] = []
        if running and self.log.latest is not None:
            replies.append(dumps_message(source_status_message(
                "running", echo, message="视频源已开始输出画面",
                frame_id=frame_id if frame_id is not None else self.log.latest.frame_id)))
        elif running and frame_id is not None:
            # 会话在跑但还没有帧（打开中）：不发"已开始输出画面"，等第一帧再说
            pass
        return {"gen": gen, "replies": replies}

    # ---------- 状态端点（既有接口的取值口径不变） ----------

    def get_stream_status(self) -> dict:
        with self._lock:
            sess = self.session
            if sess is None:
                return {"running": False, "thread_alive": False, "source": None,
                        "paused": False, "current_frame": 0,
                        "target_fps": float(VIDEO_FPS), "mode": "retail"}
            t = sess.bg_thread
            return {
                "running": sess.running,
                "thread_alive": bool(t is not None and t.is_alive()),
                "source": sess.source,
                "paused": sess.paused,
                "current_frame": sess.current_frame,
                "target_fps": sess.target_fps,
                "mode": sess.mode,
            }

    def get_emotion_camera_status(self) -> dict:
        with self._lock:
            sess = self.session
            if sess is None:
                return {"running": False, "start_time": 0.0}
            with sess.emo_lock:
                return {"running": sess.emo_running, "start_time": sess.emo_start_time}

    # ---------- 会话生命周期 ----------

    def start_session(self, viewer: Viewer | None, kind: str, params: dict,
                      echo: dict, mode: str) -> VideoSession:
        """建会话并启动处理线程（打开源、出帧都在处理线程里）。"""
        from vision import pipeline

        comps = self.components()
        with self._lock:
            self._sid += 1
            gen = self.log.new_gen()
            sess = VideoSession(self, f"s{self._sid}", gen, mode, kind, params,
                                echo, viewer.id if viewer else None)
            self.session = sess

            # 本机摄像头：本会话用本地 ROI（门店 ROB 与本地摄像头 ROI 是两套配置）
            if kind == "client":
                target_processor = comps["local_processor"]
                target_event_detector = comps["local_event_detector"]
                target_roi = comps["local_roi_mgr"]
                sess.emo_camera_id = "camera_local"
            else:
                target_processor = comps["processor"]
                target_event_detector = comps["event_detector"]
                target_roi = comps["roi_mgr"]
            sess.processor = target_processor
            sess.event_detector = target_event_detector
            sess.roi_manager = target_roi
            sess.face_emotion = comps["face_emotion"]

        sess.running = True
        sess.paused = False
        sess.target_fps = float(VIDEO_FPS)

        if mode == "retail":
            # ⚠ 台账 D16：skill 与 processor 必须共用同一个 ROI 实例（缩放口径一致），
            # 详见迁移前的注释与 tests/test_roi_scale.py
            from api.dependencies import reset_tracker
            pop_skill, anom_skill = comps["pop_skill"], comps["anom_skill"]
            if getattr(pop_skill, "roi_manager", None) is not target_roi:
                pop_skill.roi_manager = target_roi
                print(f"[WS] 热度 skill 切换到 ROI: {type(target_roi).__name__}"
                      f"（区域数 {len(target_roi.get_shelf_zones())}）")
            if getattr(anom_skill, "roi_manager", None) is not target_roi:
                anom_skill.roi_manager = target_roi
            reset_tracker()
            target_event_detector.reset()
            pop_skill.reset()
            anom_skill.reset()
            comps["emo_skill"].reset()
            target_processor.reset()
        else:
            # 表情模式：表情检测器缺失属"启动失败"，由处理线程按既有优先级报错
            from database import init_db
            init_db()
            with sess.emo_lock:
                sess.emo_running = True
                sess.emo_start_time = _time.time()
                sess.emo_sample_buffer.clear()
                sess.emo_batch_records.clear()
                # 本机摄像头与服务器摄像头分别记账，便于分段统计
                sess.emo_camera_id = ("camera_local" if kind == "client"
                                      else "camera_entrance")

        t = threading.Thread(target=pipeline.run, args=(sess,), daemon=True,
                             name=f"{mode}-processing")
        sess.bg_thread = t
        t.start()
        self._ensure_monitor()
        return sess

    def stop_session(self, requester: str | None = None, reason: str = "stop") -> str | None:
        """停掉当前会话，把"视频流已停止"作为控制事件投递给其他观看者。

        返回该消息文本（发起人自己的回执里带上它；没有会话时返回 None）。
        迁前对照：stop 分支 / 断连 finally / 看门狗 / 服务关闭走的是同一个 `_stop_internal`。
        """
        with self._lock:
            sess = self.session
        if sess is None or not sess.running:
            return None

        tail = dumps_message({"type": "status", "status": "stopped",
                              "message": "视频流已停止"})
        self._end_session(sess, tail=tail, requester=requester)
        print(f"[WS] 视频已停止（{reason}，会话 {sess.id}）")
        return tail

    def _end_session(self, sess: VideoSession, *,
                     texts: list[str] | None = None,
                     tail: str | None = None,
                     requester: str | None = None,
                     from_thread: bool = False) -> None:
        """会话收尾：置停止态 → 投递控制事件 → 释放资源。

        控制事件的语义与迁移前逐条对应：`texts` 是终止类消息（错误/播放完毕），
        `tail` 是"视频流已停止"（只发给收到过帧的观看者），`clear` 表示此前帧作废。
        """
        was_running = sess.running
        sess.running = False
        sess.paused = False
        sess.run_token = object()          # 令牌失效：处理线程下一轮即退出
        sess.ended = True
        with sess.emo_lock:
            sess.emo_running = False
            sess.emo_start_time = 0.0

        with self._lock:
            release_owner = (self.owner_viewer_id is not None
                             and self.owner_viewer_id == sess.requester
                             and not sess.first_frame_seen)
            if release_owner:
                self.owner_viewer_id = None
                self.owner_username = None
        if was_running:
            self.log.publish_control(sess.gen, texts or [], framed_tail=tail,
                                     clear=True, except_viewer=requester)

        if not from_thread:
            t = sess.bg_thread
            if t is not None and t.is_alive():
                t.join(timeout=_THREAD_JOIN_TIMEOUT)
            sess.release_capture()

    def terminate_session(self, sess: VideoSession, kind: str,
                          texts: list[str]) -> None:
        """会话自然终止（播放完毕 / 异常）：广播终止消息 + 收尾（由处理线程调用）。"""
        tail = dumps_message({"type": "status", "status": "stopped",
                              "message": "视频流已停止"})
        sess.terminal_kind = kind
        self._end_session(sess, texts=texts, tail=tail, from_thread=True)
        print(f"[WS] 会话 {sess.id} 终止：{kind}")

    # ---------- 本机摄像头归属仲裁 ----------

    def _owner_conn_alive(self) -> bool:
        """归属连接的存活探测（只对传统适配器有意义；Go 观看者靠注册表）。"""
        with self._lock:
            owner = self.viewers.get(self.owner_viewer_id or "")
        if owner is None:
            return False
        if owner.conn is None:
            return True                                  # Go 侧观看者：在场即算活
        return ws_alive(owner.conn)

    def _claim_client_camera(self, viewer: Viewer | None) -> bool:
        """尝试占用本机摄像头；返回 False 表示已被别人占着。"""
        with self._lock:
            if self.owner_viewer_id is not None:
                if self.owner_viewer_id == (viewer.id if viewer else None):
                    return True
                if self._owner_conn_alive():
                    return False
                print("[WS] 回收已断开的「本机摄像头」归属（原归属连接已不在）")
                self.owner_viewer_id = None
                self.owner_username = None
            if viewer is not None:
                self.owner_viewer_id = viewer.id
                self.owner_username = viewer.username
            return True

    def client_camera_available_for(self, viewer: Viewer | None) -> bool:
        with self._lock:
            if self.owner_viewer_id is None:
                return True
            if self.owner_viewer_id == (viewer.id if viewer else None):
                return True
            return not self._owner_conn_alive()

    def release_client_camera(self, viewer: Viewer | None) -> None:
        with self._lock:
            if viewer is None or self.owner_viewer_id == viewer.id:
                self.owner_viewer_id = None
                self.owner_username = None

    # ---------- 客户端帧注入 ----------

    def inject_client_frame_bytes(self, username: str | None, frame_bytes: bytes) -> None:
        """浏览器本机摄像头帧（/ws/client 的二进制）→ 当前会话的帧队列。

        只接受归属者的帧：否则别人的画面会混进同一个队列，被归属者的处理线程
        消费、显示成归属者自己的画面（迁移前实测过：A 停止推帧后仍收到 105/108
        帧来自 B 的摄像头）。
        """
        with self._lock:
            owner = self.owner_username
        if owner is not None and owner != username:
            return
        try:
            import cv2                           # 延迟导入（见文件头说明）
            import numpy as np

            arr = np.frombuffer(frame_bytes, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return
            h, w = frame.shape[:2]
            if w != VIDEO_CAMERA_WIDTH or h != VIDEO_CAMERA_HEIGHT:
                frame = cv2.resize(frame, (VIDEO_CAMERA_WIDTH, VIDEO_CAMERA_HEIGHT),
                                   interpolation=cv2.INTER_AREA)
            with self._lock:
                sess = self.session
            if sess is not None:
                with sess.client_frame_lock:
                    sess.client_frames.append(frame)
            # 数据可信度：客户端推帧成功解码即视为本机摄像头有数据
            try:
                from agents.data_quality import SOURCE_CLIENT, mark_frame
                mark_frame(SOURCE_CLIENT)
            except Exception:
                pass
        except Exception:
            pass

    def inject_client_frame(self, username: str | None, frame_b64: str) -> None:
        """base64 变体（WS 文本动作 `client_frame` 的历史入口）。"""
        try:
            self.inject_client_frame_bytes(username, base64.b64decode(frame_b64))
        except Exception:
            pass

    # ---------- 动作入口 ----------

    def handle_action(self, viewer: Viewer | None, msg: dict) -> ActionOutcome:
        """处理一条客户端动作消息，返回"发给发起人"的回执。

        与迁移前 api/routes/stream.py 的消息分支一一对应：归一化/守卫/仲裁的位置、
        文案、条数与顺序都保持原样。差别只在**重活已移到处理线程**（打开源、
        建立表情统计），这里只做校验与建会话。
        """
        action = str(msg.get("action", ""))
        mode = msg.get("mode", "retail")
        replies: list[str] = []
        prescreened = False
        plan_echo: dict = {}

        # 兼容前端 {type:"ping"} 的心跳格式
        if action == "ping" or msg.get("type") == "ping":
            return ActionOutcome(replies=[dumps_message(pong_message(msg.get("ts", 0)))],
                                 gen=self.log.gen)

        if action == "open_source":
            src = msg.get("source")
            try:
                import video_sources
                known: list = []
                try:
                    from agents.module_registry import get_registry
                    known = [c.get("id") for c in get_registry().list_cameras()]
                except Exception:
                    pass
                plan = video_sources.normalize(src, known)
            except Exception as e:
                code = getattr(e, "code", "bad_request")
                replies.append(dumps_message({
                    "type": "source_status", "state": "error",
                    "source": src if isinstance(src, dict) else {"kind": str(src)},
                    "code": code, "message": str(e),
                }))
                print(f"[WS] open_source 被拒 code={code}: {e}")
                return ActionOutcome(replies=replies, gen=self.log.gen)
            with self._lock:
                replaced = bool(self.session and self.session.running)
            plan_echo = plan["echo"]
            replies.append(dumps_message({
                "type": "source_status", "state": "opening",
                "source": plan_echo, "replaced": replaced,
                "message": f"正在打开来源（{plan['kind']}）…",
            }))
            print(f"[WS] open_source kind={plan['kind']} -> {plan['legacy_action']}"
                  f" replaced={replaced} echo={plan_echo}")
            action = plan["legacy_action"]
            msg = {**msg, **plan["params"], "action": action}
            prescreened = True

        elif action in ("start_webcam", "start_file", "start_client_camera"):
            # 老入口（向后兼容）：也回一条统一状态，方便前端逐步迁移
            try:
                import video_sources
                plan_echo = video_sources.echo_for_legacy(action, msg)
            except Exception:
                plan_echo = {}
            replies.append(dumps_message({
                "type": "source_status", "state": "opening",
                "source": plan_echo, "deprecated": True,
                "message": "该动作为兼容旧客户端保留，建议改用 open_source",
            }))

        if action == "client_frame":
            username = viewer.username if viewer else msg.get("username")
            self.inject_client_frame(username, msg.get("frame", ""))
            return ActionOutcome(replies=replies, gen=self.log.gen)

        if action == "start_webcam":
            return self._start(viewer, "device", {"camera_id": msg.get("camera_id", 0)},
                               plan_echo, mode, replies)

        if action == "start_file":
            file_path = msg.get("file_path", "")
            import video_sources
            from config.settings import VIDEO_LEGACY_START_FILE_STRICT
            # ⚠ 只在"这条消息不是从 open_source 翻译过来的"时才校验 ——
            # 否则服务端配置的可信路径会被客户端规则二次拒绝（已踩过）
            if VIDEO_LEGACY_START_FILE_STRICT and not prescreened:
                try:
                    file_path = str(video_sources.guard_path(file_path, client_supplied=True))
                except Exception as e:
                    code = getattr(e, "code", "bad_request")
                    message = (f"视频源不被允许[{code}]: "
                               f"{video_sources.mask_credentials(str(e))}")
                    replies.append(dumps_message({
                        "type": "status", "status": "error", "message": message,
                    }))
                    replies.append(dumps_message(source_status_message(
                        "error", plan_echo, code=code, message=message)))
                    print(f"[WS] start_file 被拒 code={code}")
                    return ActionOutcome(replies=replies, gen=self.log.gen)
            return self._start(viewer, "file", {"file_path": file_path},
                               plan_echo, mode, replies)

        if action == "start_client_camera":
            return self._start(viewer, "client", {}, plan_echo, mode, replies)

        if action in ("pause", "resume"):
            self.set_paused(action == "pause")
            return ActionOutcome(replies=replies, gen=self.log.gen)

        if action == "stop":
            return self._stop_action(viewer, replies)

        return ActionOutcome(replies=replies, gen=self.log.gen)

    # ---------- 各动作实现 ----------

    def _start(self, viewer: Viewer | None, kind: str, params: dict, echo: dict,
               mode: str, replies: list[str]) -> ActionOutcome:
        # 本机摄像头模式必须按连接独占（原因见 改进记录 · 本机摄像头串台）。
        # ⚠ 仲裁要在**停旧会话之前**：被拒绝的请求不该影响正在跑的会话。
        if kind == "client" and not self.client_camera_available_for(viewer):
            replies.append(dumps_message({
                "type": "status", "status": "error",
                # 协议面文案（唯一来源 ws_protocol.CLIENT_CAMERA_BUSY_MESSAGE，逐字冻结）
                "message": CLIENT_CAMERA_BUSY_MESSAGE,
            }))
            return ActionOutcome(replies=replies, gen=self.log.gen)

        with self._lock:
            replaced = bool(self.session and self.session.running)

        # 停旧会话：发起人自己走回执（framed_tail），其他观看者收到"视频流已停止"事件
        self.stop_session(requester=viewer.id if viewer else None, reason="replaced")

        # 占用登记（上面的可用性检查与这里之间可能被别的观看者抢先，再判一次）
        if kind == "client" and not self._claim_client_camera(viewer):
            replies.append(dumps_message({
                "type": "status", "status": "error",
                "message": CLIENT_CAMERA_BUSY_MESSAGE,
            }))
            return ActionOutcome(replies=replies, gen=self.log.gen)

        try:
            self.start_session(viewer, kind, params, echo, mode)
        except Exception as e:
            print(f"[WS] 视频处理启动失败: {e}")
            if kind == "client":
                self.release_client_camera(viewer)
            self.stop_session(requester=None, reason="start_failed")
            replies.append(dumps_message({
                "type": "status", "status": "error",
                "message": f"视频启动失败: {e}",
            }))
            return ActionOutcome(replies=replies, gen=self.log.gen)
        print(f"[WS] 会话已启动 kind={kind} mode={mode}"
              + (f" camera_id={params.get('camera_id')}" if "camera_id" in params else ""))
        # 与迁移前一致：start 之后本条连接的"已发帧号"复位（不发"视频流已停止"）
        return ActionOutcome(replies=replies, clear=True, gen=self.log.gen,
                             replaced=replaced)

    def _stop_action(self, viewer: Viewer | None, replies: list[str]) -> ActionOutcome:
        viewer_id = viewer.id if viewer else None
        # 主动停止：释放「本机摄像头」归属（归属者是发起人才释放）
        with self._lock:
            if viewer is not None and self.owner_viewer_id == viewer.id:
                self.owner_viewer_id = None
                self.owner_username = None

        # 表情模式：先停止表情摄像头并生成分段分析
        segment_analysis = None
        with self._lock:
            sess = self.session
        if sess is not None and sess.emo_running:
            try:
                emo_result = self.stop_emotion_camera()
                if emo_result.get("code") == 0:
                    segment_analysis = emo_result.get("segment_analysis")
            except Exception as e:
                print(f"[WS] 表情摄像头停止失败: {e}")

        with self._lock:
            echo = self.session.echo if self.session else {}
        tail = self.stop_session(requester=viewer_id, reason="stop")

        replies.append(dumps_message({
            "type": "status",
            "status": "stopped",
            "message": "视频已停止，WebSocket 保持连接",
            "segment_analysis": segment_analysis,
        }))
        replies.append(dumps_message(source_status_message(
            "stopped", echo, message="视频已停止")))
        return ActionOutcome(replies=replies, framed_tail=tail, clear=True,
                             gen=self.log.gen)

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            sess = self.session
        if sess is not None:
            sess.paused = paused

    # ---------- 表情摄像头（REST 的既有接口） ----------

    def stop_emotion_camera(self) -> dict:
        """停止表情摄像头（真正停止处理管线）并返回分段分析。"""
        from datetime import datetime
        from database import get_statistic, generate_emotion_analysis

        with self._lock:
            sess = self.session
        if sess is None:
            return {"code": 1, "msg": "表情摄像头当前未运行"}

        with sess.emo_lock:
            if not sess.emo_running:
                return {"code": 1, "msg": "表情摄像头当前未运行"}

            end_time = _time.time()
            total_duration = end_time - sess.emo_start_time
            mid_time = sess.emo_start_time + total_duration / 2

            start_str = datetime.fromtimestamp(sess.emo_start_time).strftime("%Y-%m-%d %H:%M:%S")
            mid_str = datetime.fromtimestamp(mid_time).strftime("%Y-%m-%d %H:%M:%S")
            end_str = datetime.fromtimestamp(end_time).strftime("%Y-%m-%d %H:%M:%S")

            cam_id = sess.emo_camera_id
            early_data = get_statistic(start_str, mid_str, cam_id)
            late_data = get_statistic(mid_str, end_str, cam_id)
            analysis_text = generate_emotion_analysis(early_data, late_data)

            # 批量入库剩余记录
            if sess.emo_batch_records:
                from database import insert_batch_records
                insert_batch_records(cam_id, sess.emo_batch_records)
                sess.emo_batch_records.clear()

            sess.emo_running = False
            sess.emo_start_time = 0.0
            sess.emo_sample_buffer.clear()

        # 真正停止视频处理管线
        self.stop_session(requester=None, reason="emotion_stop")

        return {
            "code": 0,
            "msg": "门店出入口摄像头已关闭",
            "segment_analysis": {
                "前半段(采集前期)": early_data,
                "后半段(采集后期)": late_data,
                "分析结论": analysis_text,
            },
        }

    # ---------- Go 上行流在场登记 ----------

    def edge_stream_opened(self) -> None:
        with self._lock:
            self._edge_streams += 1
            self._edge_lost_at = None

    def edge_stream_closed(self) -> None:
        with self._lock:
            self._edge_streams = max(0, self._edge_streams - 1)
            if self._edge_streams == 0:
                self._edge_lost_at = _time.time()

    # ---------- 看门狗 ----------

    def _ensure_monitor(self) -> None:
        with self._lock:
            if self._monitor_started:
                return
            self._monitor_started = True
        threading.Thread(target=self._monitor_loop, daemon=True,
                         name="session-monitor").start()

    def _monitor_loop(self) -> None:
        while not self._shutdown:
            _time.sleep(0.5)
            try:
                self._tick_monitor()
            except Exception as e:              # 看门狗自己绝不能死
                print(f"[WS] 会话看门狗异常: {e}")

    def _tick_monitor(self) -> None:
        with self._lock:
            sess = self.session
            edge_lost_at = self._edge_lost_at
        # 1)「打开超时」：到点了还没有**任何一帧**产出，观看者不该无限等下去
        if (sess is not None and sess.running and not sess.first_frame_seen
                and sess.pipeline_started_at is not None
                and _time.time() - sess.pipeline_started_at > VIDEO_OPEN_TIMEOUT_SECONDS):
            print(f"[WS] 打开超时（会话 {sess.id}）")
            self.terminate_session(sess, "error", [dumps_message(source_status_message(
                "error", sess.echo, code="open_timeout",
                message=(f"打开超时：{VIDEO_OPEN_TIMEOUT_SECONDS} 秒内没有产出画面，"
                         f"已停止。请检查视频源是否可用（或看服务端日志里的具体报错）。"),
            ))])
            return
        # 2) Go 上行流断开宽限：超时视为它登记过的观看者都已失联
        if edge_lost_at is not None and _time.time() - edge_lost_at > _EDGE_GRACE_SECONDS:
            with self._lock:
                ids = sorted(self._edge_viewers)
                self._edge_lost_at = None
            for vid in ids:
                print(f"[WS] 边缘上行流离线超时，回收观看者 {vid}")
                self.detach_viewer(vid)

    # ---------- 关闭 ----------

    def shutdown(self) -> None:
        """服务关闭：停会话（不再产生新帧）。"""
        self._shutdown = True
        sess = self.session
        if sess is not None and sess.running:
            self._end_session(sess, tail=None, requester=None)


# ==================== 进程内单例 + 兼容入口 ====================

MANAGER = SessionManager()


def get_stream_status() -> dict:
    return MANAGER.get_stream_status()


def get_emotion_camera_status() -> dict:
    return MANAGER.get_emotion_camera_status()


def stop_emotion_camera() -> dict:
    return MANAGER.stop_emotion_camera()
