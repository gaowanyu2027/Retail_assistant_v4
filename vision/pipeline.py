"""视频处理线程：读帧 → 推理 → 发布（零售 / 表情两种模式）。

从 `api/routes/stream.py` 原样迁入（迁移前是模块级函数 + 模块级全局变量），改为对
`VideoSession` 操作：

- 状态读写走会话字段（帧号 / 暂停 / 运行令牌 / 表情采样缓冲 / 目标帧率）；
- 出帧走 `session.publish_frame()` —— 组装元数据文本并推入事件日志（读者各自取用）；
- **打开视频源也在这里**（迁移前在 WS 处理线程里打开：RTSP 不可达时会阻塞数十秒，
  把这条连接的收发与心跳一起冻住）。

❗ 铁律：本模块不碰连接、不碰协议 —— WS/HTTP 只负责把事件搬出去（见
`vision/session.py` 与 `api/routes/*`）。
"""
from __future__ import annotations

import time as _time

import cv2

from config.settings import GO_OWNS_INGEST_WRITES, VIDEO_FPS, VIDEO_IDLE_SLEEP_SECONDS
from vision import ingest
from vision.session import file_analysis_plan, skip_source_frames
from ws_protocol import frame_metadata_message

# 「打开失败」哨兵：与"本机摄像头（没有服务端要打开的源）"的 None 区分开
_OPEN_FAILED = object()


def run(session) -> None:
    """处理线程入口（由 SessionManager 启动为 daemon 线程）。

    打开源 → 按模式分流 → 循环；异常在此统一兜底（迁移前 `_guard_processing_thread`
    的职责）：复位运行态并把错误交给读者，客户端收到明确提示而不是画面永久冻结。
    """
    session.pipeline_started_at = _time.time()
    try:
        cap = _open_capture(session)
        if cap is _OPEN_FAILED:
            return
        session.cap = cap
        if not session.running:                 # 打开期间被停止：不进入循环
            return
        _configure_file_pacing(session, cap)
        if session.mode == "emotion":
            # ⚠ 与迁移前同一优先级：先打开源，再看表情检测器在不在
            if session.face_emotion is None:
                _start_failure(session, "表情分析模块未加载（人脸/表情模型缺失），无法启动表情模式")
                return
            outcome = _emotion_loop(session, cap)
        else:
            outcome = _retail_loop(session, cap)
        if outcome == "finished" and session.running:
            _finished(session)
    except Exception as e:                      # 线程兜底必须捕获一切，否则又回到静默死亡
        import traceback
        print(f"[WS][ERROR] 处理线程异常退出，已复位运行态并通知客户端: "
              f"{type(e).__name__}: {e}")
        traceback.print_exc()
        _pipeline_error(session, f"{type(e).__name__}: {e}")
    finally:
        session.release_capture()


# ==================== 源的打开 ====================

def _open_capture(session):
    """按会话描述符打开视频源；失败时按迁移前的文案与顺序发控制事件。

    返回：VideoCapture / None（本机摄像头：没有服务端要打开的源）/ `_OPEN_FAILED`。
    """
    from config.settings import (
        VIDEO_CAMERA_BUFFER_SIZE, VIDEO_CAMERA_FOURCC, VIDEO_CAMERA_FPS,
        VIDEO_CAMERA_HEIGHT, VIDEO_CAMERA_WIDTH,
    )
    kind, params = session.kind, session.params

    if kind == "client":
        return None

    if kind == "device":
        camera_id = int(params.get("camera_id", 0) or 0)
        # ⚠ 与迁移前一致：打开可能长时间阻塞（不可达的采集设备/驱动），
        # 因此这个调用只在处理线程里出现，绝不进事件循环
        cap = cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            message = f"无法打开摄像头 #{camera_id}"
            _fail_open(session, "device_open_failed", message)
            return _OPEN_FAILED
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*VIDEO_CAMERA_FOURCC))
        except Exception:
            pass
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_CAMERA_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_CAMERA_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, VIDEO_CAMERA_FPS)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, VIDEO_CAMERA_BUFFER_SIZE)
        except Exception:
            pass
        print(
            f"[WS] 摄像头 #{camera_id} 已启动 (模式: {session.mode}, "
            f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
            f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}, "
            f"FPS={cap.get(cv2.CAP_PROP_FPS):.1f})"
        )
        return cap

    # file / upload / rtsp / 摄像头配置里的文件路径：cv2 直接吃路径或 URL
    import video_sources
    file_path = str(params.get("file_path", ""))
    cap = cv2.VideoCapture(file_path)
    if not cap.isOpened():
        cap.release()
        # 文案用"视频源"而不是"视频文件"：这个分支同样服务于配置里的 `rtsp://…`；
        # 凭据脱敏：`rtsp://user:pass@host` 不该出现在回执/日志里
        message = f"无法打开视频源: {video_sources.mask_credentials(file_path)}"
        _fail_open(session, "source_open_failed", message)
        return _OPEN_FAILED
    print(f"[WS] 视频文件: {video_sources.mask_credentials(file_path)} (模式: {session.mode})")
    return cap


def _fail_open(session, code: str, message: str) -> None:
    """打开失败：`{"type":"status"}` 与统一 `source_status` 两条，顺序与迁移前一致。"""
    from ws_protocol import source_status_message
    from ws_protocol import dumps_message
    session.manager.terminate_session(session, "error", [
        dumps_message({"type": "status", "status": "error", "message": message}),
        dumps_message(source_status_message("error", session.echo, code=code, message=message)),
    ])


def _start_failure(session, message: str) -> None:
    """启动阶段失败（如表情模型缺失）：只有一条 `视频启动失败: …`（与迁移前一致）。"""
    from ws_protocol import dumps_message
    session.manager.terminate_session(session, "error", [
        dumps_message({"type": "status", "status": "error",
                       "message": f"视频启动失败: {message}"}),
    ])


def _pipeline_error(session, message: str) -> None:
    """运行期异常：`视频分析异常已终止：…` + 同文的 `source_status`（pipeline_error）。"""
    from ws_protocol import dumps_message, source_status_message
    text = f"视频分析异常已终止：{message}"
    session.manager.terminate_session(session, "error", [
        dumps_message({"type": "status", "status": "error", "message": text}),
        dumps_message(source_status_message("error", session.echo,
                                            code="pipeline_error", message=text)),
    ])


def _finished(session) -> None:
    """视频播放完毕（与迁移前同一文案；表情运行态随会话收尾一并复位）。"""
    from ws_protocol import dumps_message, source_status_message
    message = "视频播放完毕"
    session.manager.terminate_session(session, "finished", [
        dumps_message({"type": "status", "status": "finished", "message": message}),
        dumps_message(source_status_message("finished", session.echo,
                                            code="finished", message=message)),
    ])


# ==================== 帧循环公共部分 ====================

def _configure_file_pacing(session, cap) -> None:
    """文件/上传源按 FILE_ANALYSIS_FPS 抽帧；其它源与表情模式保持逐帧。

    抽帧后帧序号的时间单位随之变化：`processor.fps` 与 `target_fps` 都改为
    有效帧率，skill 的停留时长与播放节奏才与源时长一致。处理慢于有效帧率时
    `_adaptive_fps_wait` 不再补睡（尽力而为），不会把播放拖得比处理速度更慢。
    """
    from config.settings import FILE_ANALYSIS_FPS

    if session.mode != "retail" or session.kind not in ("file", "upload"):
        return
    if FILE_ANALYSIS_FPS <= 0 or cap is None:
        return

    try:
        src_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    except Exception:
        src_fps = 0.0
    step, effective = file_analysis_plan(src_fps, FILE_ANALYSIS_FPS)
    session.file_step = step
    session.file_analysis_fps = effective
    if effective > 0:
        session.target_fps = effective
        if session.processor is not None:
            session.processor.fps = effective
        print(f"[WS] 文件源抽帧: 源 {src_fps:.1f}fps → 每 {step} 帧取 1 帧，"
              f"分析 {effective:.1f}fps")
    else:
        print(f"[WS] 文件源抽帧: 源帧率未知，逐帧分析（FILE_ANALYSIS_FPS={FILE_ANALYSIS_FPS}）")


def _adaptive_fps_wait(session, frame_start: float) -> None:
    """根据上一帧处理耗时自动调整目标帧率，并控制处理线程节奏。"""
    from config.settings import (
        ADAPTIVE_FPS_DOWN_FACTOR, ADAPTIVE_FPS_FAST_RATIO, ADAPTIVE_FPS_SLOW_RATIO,
        ADAPTIVE_FPS_UP_FACTOR, VIDEO_MAX_FPS, VIDEO_MIN_FPS,
    )
    elapsed = _time.time() - frame_start
    if session.file_analysis_fps > 0:
        # 文件抽帧会话：播放节奏固定为源的有效帧率。
        # 不参与自适应上调 —— 抽帧后的 1 帧就代表 1/effective 秒，按 30fps
        # 推送会让播放快于源；处理慢于该帧率时自然不补睡，播放尽力跟上。
        target_fps = session.file_analysis_fps
        session.target_fps = target_fps
        sleep_time = (1.0 / max(target_fps, 0.1)) - elapsed
        if sleep_time > 0:
            _time.sleep(sleep_time)
        return
    target_fps = session.target_fps or float(VIDEO_FPS)
    target_interval = 1.0 / max(target_fps, 0.1)

    if elapsed > target_interval * ADAPTIVE_FPS_SLOW_RATIO:
        target_fps = max(VIDEO_MIN_FPS, target_fps * ADAPTIVE_FPS_DOWN_FACTOR)
    elif elapsed < target_interval * ADAPTIVE_FPS_FAST_RATIO and target_fps < VIDEO_MAX_FPS:
        target_fps = min(VIDEO_MAX_FPS, target_fps * ADAPTIVE_FPS_UP_FACTOR)

    session.target_fps = round(target_fps, 1)
    interval = 1.0 / max(target_fps, 0.1)

    sleep_time = interval - elapsed
    if sleep_time > 0:
        _time.sleep(sleep_time)


def _encode_display(frame, width: int, quality: int) -> bytes:
    """等比缩放到输出宽度并编码 JPEG（迁移前两处同款逻辑，收成一处）。"""
    h, w = frame.shape[:2]
    if w != width:
        scale = width / w
        frame = cv2.resize(frame, (width, max(1, int(h * scale))))
    _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buffer.tobytes()


def _deliver_ingest(session, payload: dict, error_label: str) -> None:
    """把一批视频派生数据交给当前的写者（S4 的唯一切换点）。

    - `GO_OWNS_INGEST_WRITES=0`：本进程直接写 MySQL（与迁移前同一批函数）；
    - `=1`：作为 kind=4 事件推入上行流，由 Go edge 落库。
    两条路径吃的是**同一份 payload**（`vision.ingest` 整形），所以字段口径不会分叉。
    写失败只记日志、不中断处理线程 —— 与迁移前一致（数据重要，画面更重要）。
    """
    if not payload:
        return
    try:
        if GO_OWNS_INGEST_WRITES:
            session.publish_ingest(payload)
        else:
            import mysql_db
            ingest.apply_local(payload, mysql_db)
    except Exception as e:
        print(f"[MySQL] {error_label}: {e}")


# ==================== 零售模式 ====================

def _retail_loop(session, cap) -> str:
    """零售模式后台线程：完整推理管线。返回 "finished" | "stopped"。"""
    from config.settings import (
        ANOMALY_ACTIVE_THRESHOLD, CAMERA_FRAME_DROP_BATCH, CAMERA_FRAME_DROP_INTERVAL,
        RETAIL_STATS_SAVE_FRAME_INTERVAL, VIDEO_JPEG_QUALITY, VIDEO_OUTPUT_WIDTH,
    )
    # 注意：函数体内任意位置有 `from datetime import datetime` 时，
    # datetime 在整个函数作用域都是局部变量，必须在所有使用点之前导入
    from datetime import datetime

    processor = session.processor
    comps = session.manager.components()
    pop_skill, anom_skill, emo_skill = comps["pop_skill"], comps["anom_skill"], comps["emo_skill"]
    event_detector = session.event_detector
    token = session.run_token

    while True:
        if token is not session.run_token or not session.running:
            return "stopped"
        if session.paused:
            _time.sleep(VIDEO_IDLE_SLEEP_SECONDS)
            continue

        if cap is None:
            frame = session.pop_client_frame()
            if frame is None:
                _time.sleep(VIDEO_IDLE_SLEEP_SECONDS)
                continue
        else:
            ret, frame = cap.read()
            if not ret:
                return "finished"
            if session.file_step > 1:
                # 文件源抽帧：丢弃本帧之后的 step-1 帧，下一轮 read 取到的是
                # 源时间轴上 1/effective 秒之后的画面
                skip_source_frames(cap, session.file_step)
            # 镜像模式：服务器摄像头整帧水平翻转（自拍视角，检测/ROI/标注随之一致）
            frame = cv2.flip(frame, 1)

        if frame is None:
            continue

        frame_start = _time.time()
        if session.current_frame == 0:
            print(f"[WS] 零售源帧尺寸: {frame.shape[1]}x{frame.shape[0]}")

        result = processor.process_frame(frame)
        session.current_frame = result.frame_id
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
                for track in processor.track_mgr.drain_retired():
                    row = ingest.build_path_row(
                        session_id=f"video_{int(_time.time())}",
                        track_id=track.track_id,
                        visited_zones=track.visited_zones or {},
                        zone_labels=processor.roi_manager.zone_label,
                        fps=processor.fps,
                        now_text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    if row is not None:  # 至少访问 2 个区域才有动线价值
                        _deliver_ingest(session, {"paths": [row]}, "动线持久化失败")
            except Exception as e:
                print(f"[MySQL] 动线持久化失败: {e}")

        if result.frame_id % RETAIL_STATS_SAVE_FRAME_INTERVAL == 0:
            try:
                now = datetime.now()
                payload = ingest.build_retail_payload(
                    now.strftime("%Y%m%d%H%M"),
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    pop_skill.get_stats().get("zones", {}),
                )
                _deliver_ingest(session, payload, "零售热度快照写入失败")
            except Exception as e:
                print(f"[MySQL] 零售热度快照写入失败: {e}")

        try:
            alert_rows = ingest.build_alert_rows(
                anom_result.get("new_alerts", []),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            if alert_rows:
                _deliver_ingest(session, {"alerts": alert_rows}, "告警记录写入失败")
        except Exception as e:
            print(f"[MySQL] 告警记录写入失败: {e}")

        if (session.file_step <= 1
                and result.frame_id % CAMERA_FRAME_DROP_INTERVAL == 0
                and isinstance(cap, cv2.VideoCapture)):
            for _ in range(CAMERA_FRAME_DROP_BATCH):
                cap.grab()

        events = event_detector.detect(
            result.tracks, result.frame_id, processor.fps, result.timestamp)

        # 画面 JPEG：检测帧才重新编码（非检测帧沿用上一张，与迁移前一致）
        is_detection_frame = (result.frame_id % processor.frame_skip == 0)
        if is_detection_frame or session.latest_jpeg is None:
            session.latest_jpeg = _encode_display(
                result.annotated_frame, VIDEO_OUTPUT_WIDTH, VIDEO_JPEG_QUALITY)

        payload = {
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
        session.publish_frame(result.frame_id,
                              frame_metadata_message(payload, session.mode),
                              session.latest_jpeg)
        _adaptive_fps_wait(session, frame_start)


# ==================== 表情模式 ====================

def _emotion_loop(session, cap) -> str:
    """表情模式后台线程：仅做人脸检测+表情识别+SQLite入库。返回 "finished" | "stopped"。"""
    from config.settings import (
        EMOTION_JPEG_QUALITY, LOCAL_BATCH_SAVE, LOCAL_SAMPLE_FRAMES,
        SANITIZE_FACES, VIDEO_OUTPUT_WIDTH,
    )
    from database import insert_batch_records, majority_vote

    face_emotion = session.face_emotion
    token = session.run_token
    frame_id = 0

    while True:
        if token is not session.run_token or not session.running:
            return "stopped"
        if session.paused:
            _time.sleep(VIDEO_IDLE_SLEEP_SECONDS)
            continue

        if cap is None:
            frame = session.pop_client_frame()
            if frame is None:
                _time.sleep(VIDEO_IDLE_SLEEP_SECONDS)
                continue
        elif isinstance(cap, cv2.VideoCapture):
            ret, frame = cap.read()
            if not ret:
                return "finished"
        else:
            return "stopped"

        if frame is None:
            continue

        frame_start = _time.time()
        frame_id += 1
        if frame_id == 1:
            print(f"[WS] 表情源帧尺寸: {frame.shape[1]}x{frame.shape[0]}")
        session.current_frame = frame_id

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
        with session.emo_lock:
            if emotion_pairs:
                session.emo_sample_buffer.extend(emotion_pairs)

            if len(session.emo_sample_buffer) >= LOCAL_SAMPLE_FRAMES:
                buffered = list(session.emo_sample_buffer)
                session.emo_sample_buffer.clear()
                voted = majority_vote([emotion for emotion, _ in buffered])
                if voted:
                    confs = [conf for emotion, conf in buffered if emotion == voted]
                    avg_conf = round(sum(confs) / len(confs), 3) if confs else 1.0
                    session.emo_batch_records.append((voted, avg_conf))

                if len(session.emo_batch_records) >= LOCAL_BATCH_SAVE:
                    insert_batch_records(session.emo_camera_id, session.emo_batch_records)
                    session.emo_batch_records.clear()

        # 画面标注
        cv2.putText(annotated, f"Mode: Emotion  Frame:{frame_id}  Faces:{len(faces)}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)

        # 人脸脱敏合规（SANITIZE_FACES=1 时对检测到的人脸区域打码，推帧/展示不泄露人脸）
        if SANITIZE_FACES and faces:
            annotated = face_emotion.mask_faces(annotated, faces)

        session.latest_jpeg = _encode_display(annotated, VIDEO_OUTPUT_WIDTH, EMOTION_JPEG_QUALITY)
        payload = {
            "frame_id": frame_id,
            "timestamp": _time.time(),
            "tracks": [],
            "events": [],
            "active_suspicious": [],
            "faces": [{"emotion": f["emotion"], "emotion_cn": f.get("emotion_cn", f["emotion"]),
                       "conf": f["conf"]} for f in faces],
        }
        session.publish_frame(frame_id, frame_metadata_message(payload, session.mode),
                              session.latest_jpeg)
        _adaptive_fps_wait(session, frame_start)
