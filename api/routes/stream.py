"""
WS /ws/stream — 实时视频流 WebSocket 端点（双模式：零售分析 / 表情分析）

架构：后台线程处理视频 -> WebSocket 仅负责推帧
- 零售模式(retail): YOLO26l+ByteTrack -> 轨迹 -> ROI -> 热度/异常/表情技能
- 表情模式(emotion): YOLOv8n-face -> MobileNetV3表情 -> 十帧表决 -> 批量入库SQLite
"""
import asyncio
import base64
import json
import threading
import time as _time
import os
from collections import deque
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from config.settings import (
    ADAPTIVE_FPS_FAST_RATIO,
    ADAPTIVE_FPS_DOWN_FACTOR,
    ADAPTIVE_FPS_SLOW_RATIO,
    ADAPTIVE_FPS_UP_FACTOR,
    ANOMALY_ACTIVE_THRESHOLD,
    CAMERA_FRAME_DROP_BATCH,
    CAMERA_FRAME_DROP_INTERVAL,
    CLIENT_FRAME_QUEUE_MAXLEN,
    FACE_EMOTION_INTERVAL,
    FRAME_SKIP,
    LOCAL_SAMPLE_FRAMES,
    RETAIL_STATS_SAVE_FRAME_INTERVAL,
    VIDEO_CAMERA_BUFFER_SIZE,
    VIDEO_CAMERA_FOURCC,
    VIDEO_CAMERA_HEIGHT,
    VIDEO_CAMERA_WIDTH,
    VIDEO_FPS,
    VIDEO_IDLE_SLEEP_SECONDS,
    WS_FPS_LOG_INTERVAL,
    WS_METADATA_INTERVAL,
    WS_POLL_SLEEP_SECONDS,
    WS_RECEIVE_TIMEOUT_SECONDS,
    WS_SEND_INTERVAL_SECONDS,
)

router = APIRouter()

# ==================== 全局线程安全状态 ====================

_lock = threading.Lock()
# 活跃 WebSocket 集合：服务关闭时主动断开，避免 Ctrl+C 后 uvicorn
# 无限等待浏览器端的视频 WS 优雅关闭（表现为按两三次才退出）
_active_ws: set = set()
_active: dict[str, Any] = {
    "running": False,
    "paused": False,
    "source": None,
    "camera_id": 0,
    "current_frame": 0,
    "target_fps": float(VIDEO_FPS),
    "mode": "retail",  # "retail" | "emotion"
}

# 运行令牌：每次 start_processing 生成新令牌，旧处理线程发现令牌变化后立即退出，
# 避免快速切换视频源时新旧线程并发写共享状态
_run_token = object()

_latest_result: dict | None = None
_latest_frame_b64: str | None = None
# 当前处理线程的引用：供 get_stream_status() 做「线程是否真在跑」的校验，
# 避免出现"线程已死但状态仍报 running"的假象（见 _guard_processing_thread）
_bg_thread_ref: "threading.Thread | None" = None
# 「本机摄像头模式」的会话归属者（websocket 对象）。
#
# 为什么需要这个：本项目的视频处理会话是**进程级单份**的（_active / _run_token /
# _latest_result / _latest_frame_b64 / _client_frames 都是模块级），这对
# 「服务端摄像头」是合理设计——一个源、多人观看。但「本机摄像头」模式是把**每个
# 浏览器自己的摄像头帧**推进同一个 _client_frames 队列，就必然互相踩：
#
#   实测（两个真实 WS 客户端，A 停止推帧、只有 B 推蓝帧）：
#       A 收到 92 帧: {'RED': 1, 'BLUE': 91}   <- A 在看 B 的摄像头
#       B 收到 181 帧: {'RED': 90, 'BLUE': 91} <- B 连 A 阶段1 的积压帧都收到了
#   B 一点"本机摄像头"就会杀掉 A 的处理线程、清空 A 的帧队列，
#   然后 A 继续显示 B 的画面而毫无提示。
#
# 单进程 + 全局会话的架构下无法真正按连接隔离，因此这里**显式拒绝第二个会话**，
# 给出明确提示，而不是静默串台。
# （彻底解法见 改进记录.md：把会话状态收进 per-connection 会话对象。）
#
# 归属用**用户名**而不是 websocket 对象：浏览器推帧走的是**另一条连接**
# `/api/ws/client`（见 frontend-vue/public/js/stream.js:297），
# 与启动会话的 `/api/ws/stream` 不是同一个 websocket 实例，
# 只有"同一个登录用户"这个共同点能把两者关联起来。
_client_camera_owner: "str | None" = None
# 启动仲裁用：记录**启动会话的那条 /ws/stream 连接本身**。
# 仅靠用户名不够——同一个用户开两个标签页时用户名相同，第二个标签页会在
# 用户名比较中"看起来是归属者"从而抢占第一个标签页的会话。
_client_camera_owner_ws = None


def _ws_username(websocket) -> "str | None":
    """取当前 WS 连接的登录用户名。

    鉴权中间件已把用户解析进 `scope["state"]["user"]`（见 api/security.py），
    这里直接读即可——**不要**改用 user_from_scope()，它会消费一次性票据。
    """
    try:
        user = (websocket.scope.get("state") or {}).get("user") or {}
        return user.get("username")
    except Exception:
        return None

# 客户端帧队列
_client_frames: deque = deque(maxlen=CLIENT_FRAME_QUEUE_MAXLEN)
_client_frame_lock = threading.Lock()

# 表情模式专用状态
_emo_state = {
    "running": False,
    "start_time": 0.0,
    "sample_buffer": deque(maxlen=LOCAL_SAMPLE_FRAMES),
    "batch_records": [],
    "camera_id": "camera_entrance",
}
_emo_lock = threading.Lock()


def inject_client_frame(frame_b64: str):
    """注入客户端帧（浏览器摄像头 -> WebSocket -> 服务端处理）"""
    try:
        img_bytes = base64.b64decode(frame_b64)
        inject_client_frame_bytes(img_bytes)
    except Exception:
        pass


def inject_client_frame_bytes(frame_bytes: bytes):
    """注入客户端 JPEG 二进制帧"""
    try:
        arr = np.frombuffer(frame_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is not None:
            h, w = frame.shape[:2]
            if w != VIDEO_CAMERA_WIDTH or h != VIDEO_CAMERA_HEIGHT:
                frame = cv2.resize(
                    frame,
                    (VIDEO_CAMERA_WIDTH, VIDEO_CAMERA_HEIGHT),
                    interpolation=cv2.INTER_AREA,
                )
            with _client_frame_lock:
                _client_frames.append(frame)
            # 数据可信度：客户端推帧成功解码即视为本机摄像头有数据
            try:
                from agents.data_quality import SOURCE_CLIENT, mark_frame
                mark_frame(SOURCE_CLIENT)
            except Exception:
                pass
    except Exception:
        pass


def get_client_frame():
    """获取最新客户端帧"""
    with _client_frame_lock:
        if _client_frames:
            return _client_frames.popleft()
        return None


def get_stream_status() -> dict:
    with _lock:
        # 线程存活校验：`_active["running"]` 是"意图状态"，线程是否真在跑是"事实状态"。
        # 两者不一致时以事实为准（thread_alive=False 即说明处理线程已退出）。
        # _guard_processing_thread 已经会在异常时复位 running，这里是第二道保险，
        # 覆盖"线程被杀/进程内其他方式退出"等不经过 guard 的路径。
        alive = bool(_bg_thread_ref is not None and _bg_thread_ref.is_alive())
        return {
            "running": _active["running"],
            "thread_alive": alive,
            "source": _active["source"],
            "paused": _active["paused"],
            "current_frame": _active["current_frame"],
            "target_fps": _active.get("target_fps", 15.0),
            "mode": _active["mode"],
        }


def get_emotion_camera_status() -> dict:
    """获取表情摄像头状态"""
    with _emo_lock:
        return {
            "running": _emo_state["running"],
            "start_time": _emo_state["start_time"],
        }


def stop_emotion_camera() -> dict:
    """停止表情摄像头（真正停止处理管线）并返回分段分析"""
    from datetime import datetime
    from database import get_statistic, generate_emotion_analysis

    with _emo_lock:
        if not _emo_state["running"]:
            return {"code": 1, "msg": "表情摄像头当前未运行"}

        end_time = _time.time()
        total_duration = end_time - _emo_state["start_time"]
        mid_time = _emo_state["start_time"] + total_duration / 2

        start_str = datetime.fromtimestamp(_emo_state["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
        mid_str = datetime.fromtimestamp(mid_time).strftime("%Y-%m-%d %H:%M:%S")
        end_str = datetime.fromtimestamp(end_time).strftime("%Y-%m-%d %H:%M:%S")

        cam_id = _emo_state["camera_id"]
        early_data = get_statistic(start_str, mid_str, cam_id)
        late_data = get_statistic(mid_str, end_str, cam_id)
        analysis_text = generate_emotion_analysis(early_data, late_data)

        # 批量入库剩余记录
        if _emo_state["batch_records"]:
            from database import insert_batch_records
            insert_batch_records(cam_id, _emo_state["batch_records"])
            _emo_state["batch_records"].clear()

        _emo_state["running"] = False
        _emo_state["start_time"] = 0.0
        _emo_state["sample_buffer"].clear()

    # 真正停止视频处理管线：后台线程循环检查 _active["running"]
    _stop_internal()

    return {
        "code": 0,
        "msg": "门店出入口摄像头已关闭",
        "segment_analysis": {
            "前半段(采集前期)": early_data,
            "后半段(采集后期)": late_data,
            "分析结论": analysis_text
        }
    }


def _stop_internal():
    """内部停止（统一复位运行态，含表情模式状态）"""
    global _latest_result, _latest_frame_b64
    with _lock:
        _active["running"] = False
        _active["source"] = None
    with _emo_lock:
        _emo_state["running"] = False
        _emo_state["start_time"] = 0.0
    _latest_result = None
    _latest_frame_b64 = None


async def shutdown_websockets():
    """服务关闭时主动断开全部活跃 WebSocket（防止 uvicorn 无限等待优雅关闭）。"""
    for ws in list(_active_ws):
        try:
            await ws.close(code=1001, reason="server shutdown")
        except Exception:
            pass
    _active_ws.clear()


def _adaptive_fps_wait(frame_start: float):
    """根据上一帧处理耗时自动调整目标帧率，并控制处理线程节奏。"""
    from config.settings import VIDEO_MIN_FPS, VIDEO_MAX_FPS, VIDEO_FPS

    elapsed = _time.time() - frame_start
    with _lock:
        target_fps = _active.get("target_fps", VIDEO_FPS)
        target_interval = 1.0 / max(target_fps, 0.1)

        if elapsed > target_interval * ADAPTIVE_FPS_SLOW_RATIO:
            target_fps = max(VIDEO_MIN_FPS, target_fps * ADAPTIVE_FPS_DOWN_FACTOR)
        elif elapsed < target_interval * ADAPTIVE_FPS_FAST_RATIO and target_fps < VIDEO_MAX_FPS:
            target_fps = min(VIDEO_MAX_FPS, target_fps * ADAPTIVE_FPS_UP_FACTOR)

        _active["target_fps"] = round(target_fps, 1)
        interval = 1.0 / max(target_fps, 0.1)

    sleep_time = interval - elapsed
    if sleep_time > 0:
        _time.sleep(sleep_time)


# ==================== 表情模式处理线程 ====================

def _processing_thread_emotion(cap_source, face_emotion, run_token):
    """表情模式后台线程：仅做人脸检测+表情识别+SQLite入库"""
    global _latest_result, _latest_frame_b64
    if face_emotion is None:
        print("[WS] 表情检测器未加载，表情模式处理线程无法启动")
        return
    from database import insert_batch_records, majority_vote
    from config.settings import (
        LOCAL_SAMPLE_FRAMES,
        LOCAL_BATCH_SAVE,
        VIDEO_OUTPUT_WIDTH,
        EMOTION_JPEG_QUALITY,
        SANITIZE_FACES,
    )

    frame_id = 0
    while True:
        with _lock:
            if run_token is not _run_token or not _active["running"]:
                break
            paused = _active["paused"]

        if paused:
            _time.sleep(VIDEO_IDLE_SLEEP_SECONDS)
            continue

        # 读帧
        if cap_source is None:
            frame = get_client_frame()
            if frame is None:
                _time.sleep(VIDEO_IDLE_SLEEP_SECONDS)
                continue
        elif isinstance(cap_source, cv2.VideoCapture):
            ret, frame = cap_source.read()
            if not ret:
                _latest_result = {"type": "finished"}
                # 视频播完必须复位表情运行态，否则 GET/DELETE /emotion-cameras
                # 一直报告运行中，且后续统计基于过期的 start_time
                with _emo_lock:
                    _emo_state["running"] = False
                    _emo_state["start_time"] = 0.0
                break
        else:
            break

        if frame is None:
            continue

        frame_start = _time.time()
        frame_id += 1
        if frame_id == 1:
            print(f"[WS] 表情源帧尺寸: {frame.shape[1]}x{frame.shape[0]}")
        with _lock:
            _active["current_frame"] = frame_id

        # 数据可信度：表情分析有帧产出 → 上报新鲜度
        try:
            from agents.data_quality import SOURCE_EMOTION, mark_frame
            mark_frame(SOURCE_EMOTION)
        except Exception:
            pass

        annotated = frame.copy()
        emotion_pairs = []

        # 人脸检测 + 表情识别
        faces = face_emotion.detect(frame)
        for face in faces:
            x1, y1, x2, y2 = face["bbox"]
            emotion = face["emotion"]
            conf = face["conf"]
            emotion_pairs.append((emotion, conf))

            # 绘制人脸框
            color = (0, 255, 0) if emotion in ("happy", "neutral") else (0, 100, 255)
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            text = f"{face.get('emotion_cn', emotion)} {conf:.2f}"
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            cv2.rectangle(annotated, (x1, y1 - th - 10), (x1 + tw, y1), color, -1)
            cv2.putText(annotated, text, (x1, y1 - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        # 十帧多数表决 + 批量入库
        # 置信度取该表情在表决窗口内的平均值，避免入库置信度恒为 1.0
        with _emo_lock:
            if emotion_pairs:
                _emo_state["sample_buffer"].extend(emotion_pairs)

            if len(_emo_state["sample_buffer"]) >= LOCAL_SAMPLE_FRAMES:
                buffered = list(_emo_state["sample_buffer"])
                _emo_state["sample_buffer"].clear()
                voted = majority_vote([emotion for emotion, _ in buffered])
                if voted:
                    confs = [conf for emotion, conf in buffered if emotion == voted]
                    avg_conf = round(sum(confs) / len(confs), 3) if confs else 1.0
                    _emo_state["batch_records"].append((voted, avg_conf))

                if len(_emo_state["batch_records"]) >= LOCAL_BATCH_SAVE:
                    insert_batch_records(_emo_state["camera_id"], _emo_state["batch_records"])
                    _emo_state["batch_records"].clear()

        # 画面标注
        cv2.putText(annotated, f"Mode: Emotion  Frame:{frame_id}  Faces:{len(faces)}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)

        # 编码帧：统一宽度为 VIDEO_OUTPUT_WIDTH，保留原始宽高比
        h, w = annotated.shape[:2]
        if w != VIDEO_OUTPUT_WIDTH:
            scale = VIDEO_OUTPUT_WIDTH / w
            annotated = cv2.resize(annotated, (VIDEO_OUTPUT_WIDTH, max(1, int(h * scale))))
        # 人脸脱敏合规（SANITIZE_FACES=1 时对检测到的人脸区域打码，推帧/展示不泄露人脸）
        if SANITIZE_FACES and faces:
            annotated = face_emotion.mask_faces(annotated, faces)
        _, buffer = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, EMOTION_JPEG_QUALITY])
        _latest_frame_b64 = base64.b64encode(buffer).decode()

        _latest_result = {
            "frame_id": frame_id,
            "timestamp": _time.time(),
            "tracks": [],
            "events": [],
            "active_suspicious": [],
            "faces": [{"emotion": f["emotion"], "emotion_cn": f.get("emotion_cn", f["emotion"]),
                        "conf": f["conf"]} for f in faces],
        }
        _adaptive_fps_wait(frame_start)


# ==================== 零售模式处理线程 ====================

def _guard_processing_thread(fn, *args) -> None:
    """处理线程的异常兜底。

    为什么必须有：两个处理线程（零售 / 表情）的循环体**原本没有任何 try/except**，
    于是任意一帧里的异常（推理、ROI 遍历、skill、DB、模型）都会让线程**静默死亡**——
    而 `_active["running"]` 不会复位。实测复现（第 30 帧抛异常）：

        Exception in thread retail-processing:
          File "/app/api/routes/stream.py", line 419, in _processing_thread_retail
        RuntimeError: 模拟推理阶段异常（第 30 帧）

        处理线程还活着吗 : False
        get_stream_status: {'running': True, ..., 'current_frame': 29}

    即：画面永久冻结、`/api/streams/status` 一直报"运行中"、客户端收不到任何错误，
    必须人工重启服务。这与 ROI 并发改写（见 cv_engine/roi_manager.py 的
    copy-on-write 修复）叠加时**必现**：用户在 ROI 页面新增一个区域即可触发。

    这里在**线程入口统一兜底**（一处覆盖两种模式），并复用既有的 `_latest_result`
    约定把错误交给 WebSocket 循环 → 客户端收到明确的 error 状态，运行态同时复位。
    """
    global _latest_result
    try:
        fn(*args)
    except Exception as e:  # 线程兜底必须捕获一切，否则又回到静默死亡
        import traceback
        print(f"[WS][ERROR] 处理线程异常退出，已复位运行态并通知客户端: "
              f"{type(e).__name__}: {e}")
        traceback.print_exc()
        with _lock:
            _active["running"] = False
            _active["paused"] = False
        try:
            with _emo_lock:
                _emo_state["running"] = False
        except Exception:
            pass
        # 复用 finished 的通道：WS 循环看到 type=error 会通知客户端并清理资源
        _latest_result = {"type": "error",
                          "message": f"{type(e).__name__}: {e}"}


def _processing_thread_retail(cap_source, processor, pop_skill, anom_skill, emo_skill, event_detector, run_token):
    """零售模式后台线程：完整推理管线"""
    global _latest_result, _latest_frame_b64
    from config.settings import VIDEO_OUTPUT_WIDTH, VIDEO_JPEG_QUALITY
    # 注意：函数体内任意位置有 `from datetime import datetime` 时，
    # datetime 在整个函数作用域都是局部变量，必须在所有使用点之前导入
    from datetime import datetime

    while True:
        with _lock:
            if run_token is not _run_token or not _active["running"]:
                break
            paused = _active["paused"]

        if paused:
            _time.sleep(VIDEO_IDLE_SLEEP_SECONDS)
            continue

        if cap_source is None:
            frame = get_client_frame()
            if frame is None:
                _time.sleep(VIDEO_IDLE_SLEEP_SECONDS)
                continue
        elif isinstance(cap_source, cv2.VideoCapture):
            ret, frame = cap_source.read()
            if not ret:
                _latest_result = {"type": "finished"}
                # 视频播完必须复位表情运行态，否则 GET/DELETE /emotion-cameras
                # 一直报告运行中，且后续统计基于过期的 start_time
                with _emo_lock:
                    _emo_state["running"] = False
                    _emo_state["start_time"] = 0.0
                break
            # 镜像模式：服务器摄像头整帧水平翻转（自拍视角，检测/ROI/标注随之一致）
            frame = cv2.flip(frame, 1)
        else:
            break

        if frame is None:
            continue

        frame_start = _time.time()
        if _active.get("current_frame", 0) == 0:
            print(f"[WS] 零售源帧尺寸: {frame.shape[1]}x{frame.shape[0]}")

        result = processor.process_frame(frame)
        with _lock:
            _active["current_frame"] = result.frame_id
        # 数据可信度：零售分析有帧产出 → 上报新鲜度
        try:
            from agents.data_quality import SOURCE_RETAIL, mark_frame
            mark_frame(SOURCE_RETAIL)
        except Exception:
            pass

        pop_skill.process(
            result.tracks, result.frame_id, processor.fps,
            timestamp=result.timestamp,
            current_hour=datetime.now().strftime("%Y%m%d%H"),  # 含日期，跨天不混
        )
        anom_result = anom_skill.process(result.tracks, result.frame_id, processor.fps)
        emo_skill.process(result.tracks, result.timestamp)

        # 数据隔离：同步喂给 module_registry 当前绑定的摄像头模块（独立 skill 实例）
        try:
            from agents.module_registry import get_registry
            get_registry().feed_current_frame(
                result.tracks, result.frame_id, processor.fps,
                timestamp=result.timestamp,
                current_hour=datetime.now().strftime("%Y%m%d%H"),
            )
        except Exception as e:
            print(f"[ModuleRegistry] 喂帧到模块失败: {e}")

        # 顾客动线持久化：处理线程把"被清理的轨迹"（离开画面/丢失超限）的访问序列落库，
        # 供购物动线分析（A→B 关联规则）使用。CV 层零 DB 依赖，由本层 drain 后写入。
        if result.frame_id % 30 == 0:  # 约每 1 秒 drain 一次（retired 为空时零开销）
            try:
                import mysql_db
                for track in processor.track_mgr.drain_retired():
                    path, total_dwell = [], 0.0
                    for zid, rec in (track.visited_zones or {}).items():
                        label = processor.roi_manager.zone_label.get(zid, zid)
                        dwell = 0.0
                        if rec.get("enter_ts") is not None and rec.get("last_ts") is not None:
                            dwell = max(rec["last_ts"] - rec["enter_ts"], 0.0)
                        elif rec.get("dwell_frames"):
                            dwell = rec["dwell_frames"] / max(processor.fps, 1.0)
                        if dwell > 0.5:  # 过滤过短停留
                            path.append({"zone_id": zid, "zone_label": label,
                                         "dwell_seconds": round(dwell, 1)})
                            total_dwell += dwell
                    if len(path) >= 2:  # 至少访问 2 个区域才有动线价值
                        mysql_db.save_track_visit_path(
                            session_id=f"video_{int(_time.time())}",
                            track_id=track.track_id,
                            path=path, source="video",
                            total_dwell_seconds=round(total_dwell, 1),
                        )
            except Exception as e:
                print(f"[MySQL] 动线持久化失败: {e}")

        if result.frame_id % RETAIL_STATS_SAVE_FRAME_INTERVAL == 0:
            try:
                import mysql_db
                stats = pop_skill.get_stats()
                mysql_db.save_retail_stats(
                    datetime.now().strftime("%Y%m%d%H%M"),
                    stats.get("zones", {}),
                )
            except Exception as e:
                print(f"[MySQL] 零售热度快照写入失败: {e}")

        for alert in anom_result.get("new_alerts", []):
            try:
                import mysql_db
                mysql_db.save_alert_record(
                    alert_type="anomaly",
                    zone_id=(alert.get("zone_visited") or [""])[0],
                    person_id=alert.get("person_id", 0),
                    level=alert.get("level", "watch"),
                    score=alert.get("score", 0),
                    reason="；".join(alert.get("reasons", [])),
                    frame_id=alert.get("frame_id", 0),
                    created_at=alert.get("timestamp") or None,
                )
            except Exception as e:
                print(f"[MySQL] 告警记录写入失败: {e}")

        if result.frame_id % CAMERA_FRAME_DROP_INTERVAL == 0 and isinstance(cap_source, cv2.VideoCapture):
            for _ in range(CAMERA_FRAME_DROP_BATCH):
                cap_source.grab()

        events = event_detector.detect(
            result.tracks, result.frame_id, processor.fps, result.timestamp)

        is_detection_frame = (result.frame_id % processor.frame_skip == 0)
        if is_detection_frame or _latest_frame_b64 is None:
            display_frame = result.annotated_frame
            h, w = display_frame.shape[:2]
            if w != VIDEO_OUTPUT_WIDTH:
                scale = VIDEO_OUTPUT_WIDTH / w
                display_frame = cv2.resize(display_frame, (VIDEO_OUTPUT_WIDTH, max(1, int(h * scale))))
            _, buffer = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, VIDEO_JPEG_QUALITY])
            _latest_frame_b64 = base64.b64encode(buffer).decode()

        _latest_result = {
            "frame_id": result.frame_id,
            "timestamp": result.timestamp,
            "tracks": [{"track_id": t.track_id, "center": list(t.center),
                         "is_staff": t.is_staff, "anomaly_score": t.anomaly_score}
                        for t in result.tracks],
            "events": [{"type": e.event_type, "zone_id": e.zone_id,
                        "track_id": e.track_id, "detail": e.detail}
                       for e in events],
            "active_suspicious": [
                {"track_id": t.track_id, "score": t.anomaly_score}
                for t in result.tracks if t.anomaly_score >= ANOMALY_ACTIVE_THRESHOLD
            ],
        }
        _adaptive_fps_wait(frame_start)


# ==================== WebSocket 端点 ====================

async def _emit_source_status(websocket, state: str, echo: dict | None = None,
                              code: str | None = None, message: str | None = None, **extra):
    """发送**统一状态回执** `source_status`（A 档）。

    为什么与老的 `{"type":"status"}` 并存：老前端只认 status，新前端需要
    **机器可读的 code** 与**源回显**（谁在跑、为什么失败）。两者同时发，迁移期零破坏。
    """
    payload = {"type": "source_status", "state": state, "source": echo or {}}
    if code:
        payload["code"] = code
    if message:
        payload["message"] = message
    payload.update(extra)
    try:
        await websocket.send_json(payload)
    except Exception as e:                      # 连接已断：不能影响主流程
        print(f"[WS] source_status 发送失败: {e}")


@router.get("/video/sources")
async def list_video_sources():
    """**视频输入能力发现**（A 档）：前端据此渲染菜单/置灰并说明原因。

    返回每类来源是否可用（例如容器里"本机设备"必然不可用）、
    已注册摄像头逐台的可用性与原因、以及上传/文件的限制。
    """
    import video_sources
    try:
        return await asyncio.to_thread(video_sources.capabilities)   # 含文件 stat，丢线程池
    except Exception as e:
        print(f"[VideoSource] 能力查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"视频源能力查询失败: {e}")


@router.websocket("/ws/client")
async def client_camera_stream(websocket: WebSocket):
    """接收浏览器本机摄像头 JPEG 二进制帧，交给主处理线程消费。"""
    await websocket.accept()
    print("[WS] 本机摄像头二进制流已连接")
    _active_ws.add(websocket)
    await websocket.send_json({"event": "ready"})

    username = _ws_username(websocket)
    try:
        while True:
            frame_bytes = await websocket.receive_bytes()
            if not frame_bytes:
                continue
            # 只接受「本机摄像头」会话归属者的帧。
            # 否则其他用户的帧会混进同一个 _client_frames 队列，被归属者的
            # 处理线程消费、显示成归属者自己的画面（实测：A 停止推帧后仍收到
            # 105/108 帧来自 B 的摄像头）。
            with _lock:
                owner = _client_camera_owner
            if owner is not None and owner != username:
                continue
            inject_client_frame_bytes(frame_bytes)
    except WebSocketDisconnect:
        pass
    finally:
        _active_ws.discard(websocket)
        print("[WS] 本机摄像头二进制流已断开")


@router.websocket("/ws/stream")
async def video_stream(websocket: WebSocket):
    # 统一在函数顶部声明（同一函数内重复 global 声明会触发
    # "name is used prior to global declaration" 语法错误；
    # 而漏声明则会被当成局部变量，赋值后再读取会 UnboundLocalError）
    global _latest_result, _latest_frame_b64, _client_camera_owner, _client_camera_owner_ws

    await websocket.accept()
    print("[WS] 客户端已连接")
    # ⚠ 必须登记进 _active_ws：shutdown 时靠它主动断开所有 WS，
    # 否则 uvicorn 会一直等浏览器优雅关闭（只能靠 timeout_graceful_shutdown=5 兜底）。
    # 此前只有 /ws/client 登记、**主视频 WS 恰好漏了**——而它才是最需要被断开的那条。
    _active_ws.add(websocket)

    _latest_result = None
    _latest_frame_b64 = None

    from api.dependencies import (
        get_detector, get_roi_manager, reset_tracker,
        get_popularity_skill, get_anomaly_skill, get_emotion_skill,
        get_face_emotion,
    )
    from api.events import VideoEventDetector
    from cv_engine.tracker import TrackStateManager
    from cv_engine.video_processor import VideoProcessor

    # 零售模式组件（懒加载）
    detector = get_detector()
    roi_mgr = get_roi_manager()
    pop_skill = get_popularity_skill()
    anom_skill = get_anomaly_skill()
    emo_skill = get_emotion_skill()

    # 表情检测器（两种模式共用）
    face_emotion = None
    try:
        face_emotion = get_face_emotion()
    except Exception as e:
        print(f"[WS] 表情分析模块加载失败: {e}")

    track_mgr = TrackStateManager()
    processor = VideoProcessor(detector, track_mgr, roi_mgr, frame_skip=FRAME_SKIP,
                               face_emotion=face_emotion, face_emotion_interval=FACE_EMOTION_INTERVAL)
    event_detector = VideoEventDetector(roi_mgr)

    from config.settings import LOCAL_ROI_CONFIG_PATH, PROJECT_ROOT
    from cv_engine.roi_manager import ROIManager
    local_roi_mgr = ROIManager(str(PROJECT_ROOT / LOCAL_ROI_CONFIG_PATH))
    local_processor = VideoProcessor(detector, track_mgr, local_roi_mgr, frame_skip=FRAME_SKIP,
                                     face_emotion=face_emotion, face_emotion_interval=FACE_EMOTION_INTERVAL)
    local_event_detector = VideoEventDetector(local_roi_mgr)

    bg_thread: threading.Thread | None = None
    cap: cv2.VideoCapture | None = None
    _last_pushed_frame_id = -1
    _push_log_time = _time.time()
    _push_count = 0

    def start_processing(cap_source, mode="retail", use_processor=None, use_event_detector=None):
        """启动后台处理线程"""
        nonlocal bg_thread
        global _run_token, _latest_result, _latest_frame_b64, _bg_thread_ref
        target_processor = use_processor or processor
        target_event_detector = use_event_detector or event_detector
        _stop_internal()
        if bg_thread and bg_thread.is_alive():
            bg_thread.join(timeout=2.0)

        # 生成新的运行令牌：即使旧线程未及时退出，也会在下一轮循环检测到令牌变化而退出
        token = object()
        with _lock:
            _run_token = token
            _active["running"] = True
            _active["paused"] = False
            _active["mode"] = mode
            from config.settings import VIDEO_FPS
            _active["target_fps"] = float(VIDEO_FPS)

        nonlocal _last_pushed_frame_id
        _latest_result = None
        _latest_frame_b64 = None
        _last_pushed_frame_id = -1

        if mode == "retail":
            reset_tracker()
            target_event_detector.reset()
            pop_skill.reset()
            anom_skill.reset()
            emo_skill.reset()
            target_processor.reset()
            bg_thread = threading.Thread(
                target=_guard_processing_thread,
                args=(_processing_thread_retail, cap_source, target_processor, pop_skill,
                      anom_skill, emo_skill, target_event_detector, token),
                daemon=True,
                name="retail-processing",
            )
        else:
            # 表情模式
            if face_emotion is None:
                raise RuntimeError("表情分析模块未加载（人脸/表情模型缺失），无法启动表情模式")
            from database import init_db
            init_db()
            with _emo_lock:
                _emo_state["running"] = True
                _emo_state["start_time"] = _time.time()
                _emo_state["sample_buffer"].clear()
                _emo_state["batch_records"].clear()
                # 本机摄像头与服务器摄像头分别记账，便于分段统计
                _emo_state["camera_id"] = "camera_local" if cap_source is None else "camera_entrance"
            bg_thread = threading.Thread(
                target=_guard_processing_thread,
                args=(_processing_thread_emotion, cap_source, face_emotion, token),
                daemon=True,
                name="emotion-processing",
            )
        bg_thread.start()
        # 记录引用，供 get_stream_status() 校验线程是否真在运行
        _bg_thread_ref = bg_thread

    try:
        # 统一状态回执里要带的"源描述符"（open_source 或老动作入口都会设置）
        source_echo: dict = {}
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_RECEIVE_TIMEOUT_SECONDS)
                msg = json.loads(raw)
                action = msg.get("action", "")
                mode = msg.get("mode", "retail")

                # 兼容前端发送 {type:"ping"} 的心跳格式
                if action == "ping" or msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong", "ts": msg.get("ts", 0)})
                    continue

                # ===== 统一视频输入入口（A 档）=====
                # 客户端只发 {action:"open_source", source:{kind:...}}；这里把它**翻译**成
                # 下面的老动作，复用既有的三条启动路径（不动管线，风险最小）。
                # 校验/归一化逻辑全在 video_sources.normalize（含 B6 的目录白名单）。
                if action == "open_source":
                    src = msg.get("source")
                    try:
                        import video_sources
                        _known = []
                        try:
                            from agents.module_registry import get_registry
                            _known = [c.get("id") for c in get_registry().list_cameras()]
                        except Exception:
                            pass
                        # 可能涉及文件系统 stat / 配置读取，丢线程池
                        plan = await asyncio.to_thread(video_sources.normalize, src, _known)
                    except Exception as e:
                        code = getattr(e, "code", "bad_request")
                        await websocket.send_json({
                            "type": "source_status", "state": "error",
                            "source": src if isinstance(src, dict) else {"kind": str(src)},
                            "code": code, "message": str(e),
                        })
                        print(f"[WS] open_source 被拒 code={code}: {e}")
                        continue
                    with _lock:
                        replaced = bool(_active.get("running"))
                    source_echo = plan["echo"]
                    await websocket.send_json({
                        "type": "source_status", "state": "opening",
                        "source": source_echo, "replaced": replaced,
                        "message": f"正在打开来源（{plan['kind']}）…",
                    })
                    print(f"[WS] open_source kind={plan['kind']} -> {plan['legacy_action']}"
                          f" replaced={replaced} echo={source_echo}")
                    # 翻译成老动作后**继续往下走**，复用既有启动分支
                    action = plan["legacy_action"]
                    msg = {**msg, **plan["params"], "action": action}

                elif action in ("start_webcam", "start_file", "start_client_camera"):
                    # 老入口（向后兼容）：也回一条统一状态，方便前端逐步迁移
                    try:
                        import video_sources
                        source_echo = video_sources.echo_for_legacy(action, msg)
                    except Exception:
                        source_echo = {}
                    await websocket.send_json({
                        "type": "source_status", "state": "opening",
                        "source": source_echo, "deprecated": True,
                        "message": "该动作为兼容旧客户端保留，建议改用 open_source",
                    })

                if action == "client_frame":
                    # 同 /ws/client：只接受归属者的帧（见 _client_camera_owner 注释）
                    with _lock:
                        owner = _client_camera_owner
                    if owner is None or owner == _ws_username(websocket):
                        inject_client_frame(msg.get("frame", ""))
                    continue

                if action == "start_webcam":
                    camera_id = msg.get("camera_id", 0)
                    if cap:
                        cap.release()
                    # ⚠ 丢线程池：VideoCapture 打开失败/源不可达时可能阻塞很久
                    # （RTSP 尤甚），而这里在**事件循环**里 await —— 直接调会
                    # 把整站拖停（HTTP/心跳/其他 WS 全部无响应）。
                    cap = await asyncio.to_thread(cv2.VideoCapture, camera_id, cv2.CAP_DSHOW)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        await websocket.send_json({
                            "type": "status", "status": "error",
                            "message": f"无法打开摄像头 #{camera_id}",
                        })
                        await _emit_source_status(
                            websocket, "error", source_echo, code="device_open_failed",
                            message=f"无法打开摄像头 #{camera_id}",
                        )
                        continue
                    try:
                        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*VIDEO_CAMERA_FOURCC))
                    except Exception:
                        pass
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_CAMERA_WIDTH)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_CAMERA_HEIGHT)
                    from config.settings import VIDEO_CAMERA_FPS
                    cap.set(cv2.CAP_PROP_FPS, VIDEO_CAMERA_FPS)
                    try:
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, VIDEO_CAMERA_BUFFER_SIZE)
                    except Exception:
                        pass
                    with _lock:
                        _active["source"] = "webcam"
                        _active["camera_id"] = camera_id
                    try:
                        start_processing(cap, mode)
                    except Exception as e:
                        print(f"[WS] 摄像头处理启动失败: {e}")
                        _stop_internal()
                        cap.release()
                        cap = None
                        await websocket.send_json({
                            "type": "status", "status": "error",
                            "message": f"视频启动失败: {e}",
                        })
                        continue
                    print(
                        f"[WS] 摄像头 #{camera_id} 已启动 (模式: {mode}, "
                        f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
                        f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}, "
                        f"FPS={cap.get(cv2.CAP_PROP_FPS):.1f})"
                    )

                elif action == "start_file":
                    file_path = msg.get("file_path", "")
                    if cap:
                        cap.release()
                    # 同上：文件/RTSP 的打开一律丢线程池（RTSP 不可达时阻塞可达数十秒）
                    cap = await asyncio.to_thread(cv2.VideoCapture, file_path)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        # 文案用"视频源"而不是"视频文件"：这个分支同样服务于
                        # 摄像头配置里的 `rtsp://…` 源（cv2 打开 URL 走的就是它）
                        await websocket.send_json({
                            "type": "status", "status": "error",
                            "message": f"无法打开视频源: {file_path}",
                        })
                        await _emit_source_status(
                            websocket, "error", source_echo, code="source_open_failed",
                            message=f"无法打开视频源: {file_path}",
                        )
                        continue
                    with _lock:
                        _active["source"] = "file"
                    try:
                        start_processing(cap, mode)
                    except Exception as e:
                        print(f"[WS] 文件处理启动失败: {e}")
                        _stop_internal()
                        cap.release()
                        cap = None
                        await websocket.send_json({
                            "type": "status", "status": "error",
                            "message": f"视频启动失败: {e}",
                        })
                        continue
                    print(f"[WS] 视频文件: {file_path} (模式: {mode})")

                elif action == "start_client_camera":
                    # 本机摄像头模式必须按连接独占（原因见 _client_camera_owner 注释）。
                    # 单进程 + 全局会话的架构下无法真正隔离，故显式拒绝第二个会话，
                    # 给出明确提示，而不是静默串台。
                    uname = _ws_username(websocket)
                    with _lock:
                        owner_ws = _client_camera_owner_ws
                    if owner_ws is not None and owner_ws is not websocket:
                        await websocket.send_json({
                            "type": "status", "status": "error",
                            "message": "已有其他用户正在使用「本机摄像头」模式，请稍后重试；"
                                       "多人同时观看请使用「服务器摄像头」。",
                        })
                        continue
                    if cap:
                        cap.release()
                        cap = None
                    with _client_frame_lock:
                        _client_frames.clear()
                    with _lock:
                        _active["source"] = "client"
                        _client_camera_owner = uname
                        _client_camera_owner_ws = websocket
                    try:
                        start_processing(None, mode, local_processor, local_event_detector)
                    except Exception as e:
                        print(f"[WS] 浏览器摄像头处理启动失败: {e}")
                        with _lock:
                            _client_camera_owner = None
                        _stop_internal()
                        await websocket.send_json({
                            "type": "status", "status": "error",
                            "message": f"视频启动失败: {e}",
                        })
                        continue
                    print(f"[WS] 浏览器摄像头模式已启动 (模式: {mode})")

                elif action == "pause":
                    with _lock:
                        _active["paused"] = True

                elif action == "resume":
                    with _lock:
                        _active["paused"] = False

                elif action == "stop":
                    # 主动停止：释放「本机摄像头」归属（见 _client_camera_owner 注释）
                    with _lock:
                        if _client_camera_owner_ws is websocket:
                            _client_camera_owner = None
                            _client_camera_owner_ws = None
                    # 如果是表情模式，先停止表情摄像头并生成分段分析
                    segment_analysis = None
                    with _emo_lock:
                        emo_was_running = _emo_state["running"]
                    if emo_was_running:
                        try:
                            # DB 统计查询为同步阻塞，丢线程池避免卡事件循环
                            emo_result = await asyncio.to_thread(stop_emotion_camera)
                            if emo_result.get("code") == 0:
                                segment_analysis = emo_result.get("segment_analysis")
                        except Exception as e:
                            print(f"[WS] 表情摄像头停止失败: {e}")

                    _stop_internal()
                    if bg_thread and bg_thread.is_alive():
                        bg_thread.join(timeout=1.0)
                    if cap:
                        cap.release()
                        cap = None
                    _latest_result = None
                    _latest_frame_b64 = None
                    await websocket.send_json({
                        "type": "status",
                        "status": "stopped",
                        "message": "视频已停止，WebSocket 保持连接",
                        "segment_analysis": segment_analysis,
                    })
                    await _emit_source_status(websocket, "stopped", source_echo,
                                              message="视频已停止")
                    print("[WS] 视频已停止")

            except asyncio.TimeoutError:
                pass

            # 推送最新帧
            frame_b64 = _latest_frame_b64
            result_to_send = _latest_result

            if frame_b64 is None or result_to_send is None:
                if _last_pushed_frame_id >= 0:
                    await websocket.send_json({
                        "type": "status", "status": "stopped",
                        "message": "视频流已停止",
                    })
                    _last_pushed_frame_id = -1
                await asyncio.sleep(WS_POLL_SLEEP_SECONDS)
                continue

            # 处理线程异常退出的通知通道（由 _guard_processing_thread 写入）：
            # 必须显式告知客户端，否则画面只是"卡住"，用户完全不知道发生了什么。
            if result_to_send.get("type") == "error":
                await websocket.send_json({
                    "type": "status", "status": "error",
                    "message": f"视频分析异常已终止：{result_to_send.get('message', '')}",
                })
                await _emit_source_status(
                    websocket, "error", source_echo, code="pipeline_error",
                    message=f"视频分析异常已终止：{result_to_send.get('message', '')}",
                )
                _stop_internal()
                _latest_result = None
                _latest_frame_b64 = None
                continue

            if result_to_send.get("type") == "finished":
                await websocket.send_json({
                    "type": "status", "status": "finished",
                    "message": "视频播放完毕",
                })
                await _emit_source_status(websocket, "finished", source_echo,
                                          code="finished", message="视频播放完毕")
                _stop_internal()
                _latest_result = None
                _latest_frame_b64 = None
                continue

            new_frame_id = result_to_send.get("frame_id", -1)
            if new_frame_id <= _last_pushed_frame_id:
                await asyncio.sleep(WS_POLL_SLEEP_SECONDS)
                continue
            # 第一帧 = "源真的出画面了"的**唯一可信证据**：服务端成功时不另发回执，
            # 前端因此不必猜（修复前靠"点击后写运行中"来撒谎）。
            _first_frame = _last_pushed_frame_id < 0
            _last_pushed_frame_id = new_frame_id
            if _first_frame:
                await _emit_source_status(
                    websocket, "running", source_echo,
                    message="视频源已开始输出画面", frame_id=new_frame_id,
                )

            msg_to_send = {
                "type": "frame",
                "frame_id": result_to_send["frame_id"],
                "timestamp": result_to_send.get("timestamp", 0),
                "tracks": result_to_send.get("tracks", []),
                "events": result_to_send.get("events", []),
                "active_suspicious": result_to_send.get("active_suspicious", []),
                "mode": _active.get("mode", "retail"),
            }
            # 表情模式额外推送人脸信息
            if "faces" in result_to_send:
                msg_to_send["faces"] = result_to_send["faces"]

            if new_frame_id == 1 or new_frame_id % WS_METADATA_INTERVAL == 0:
                await websocket.send_json(msg_to_send)
            try:
                frame_bytes = base64.b64decode(frame_b64)
                frame_header = int(new_frame_id).to_bytes(4, "big")
                await websocket.send_bytes(frame_header + frame_bytes)
            except Exception as e:
                print(f"[WS] 二进制帧发送失败: {e}")
            _push_count += 1
            now = _time.time()
            if now - _push_log_time >= WS_FPS_LOG_INTERVAL:
                push_fps = _push_count / (now - _push_log_time)
                print(
                    f"[WS] 推送FPS={push_fps:.1f} "
                    f"target_fps={_active.get('target_fps')} "
                    f"frame_id={new_frame_id}"
                )
                _push_log_time = now
                _push_count = 0
            await asyncio.sleep(WS_SEND_INTERVAL_SECONDS)

    except WebSocketDisconnect:
        print("[WS] 客户端断开连接")
    except Exception as e:
        print(f"[WS] 错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        _active_ws.discard(websocket)
        # 断连即释放「本机摄像头」归属，否则该模式下会一直拒绝其他用户
        with _lock:
            if _client_camera_owner_ws is websocket:
                _client_camera_owner = None
                _client_camera_owner_ws = None
        _stop_internal()
        with _emo_lock:
            _emo_state["running"] = False
        if bg_thread and bg_thread.is_alive():
            bg_thread.join(timeout=1.0)
        if cap:
            cap.release()
        print("[WS] 连接关闭")
