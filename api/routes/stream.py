"""WS /ws/stream 与 /ws/client —— 传统适配器（浏览器直连 Python 时走这里）。

架构：处理线程（`vision/pipeline.py`）出帧 → 会话事件日志（`vision/session.py`）
→ 本文件把事件搬给这条连接。会话状态、消息文案、客户端摄像头归属仲裁都在
`vision/session.py`（**唯一来源**，Go 边缘与这里共用同一套）。

本文件只做三件事：

1. `/ws/stream`：收客户端动作 → 交给会话管理 → 把事件游标里的事件按迁移前的
   节奏（收 5ms / 发 5ms / 空转 10ms）发给这一个观看者；
2. `/ws/client`：本机摄像头的二进制帧 → 会话的帧队列；
3. `/video/sources`：视频输入能力发现（REST，未随数据面迁移）。

保留的理由：`EDGE_VIDEO=0`（或 Go 边缘不可用）时要能**秒级回退**到浏览器直连
Python；两条路径的消息字节完全一致（同一份组装代码），差异只在"谁做扇出"。
"""
import asyncio
import json
import time as _time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from config.settings import (
    WS_FPS_LOG_INTERVAL,
    WS_POLL_SLEEP_SECONDS,
    WS_RECEIVE_TIMEOUT_SECONDS,
    WS_SEND_INTERVAL_SECONDS,
)
from vision.session import (
    MANAGER,
    ActionOutcome,
    ControlEvent,
    FrameEvent,
    get_emotion_camera_status,
    get_stream_status,
    stop_emotion_camera,
    ws_alive,
)
from ws_protocol import frame_packet, should_send_metadata

router = APIRouter()

# 活跃 WebSocket 集合：服务关闭时主动断开，避免 Ctrl+C 后 uvicorn
# 无限等待浏览器端的视频 WS 优雅关闭（表现为按两三次才退出）
_active_ws: set = set()


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


def _stop_internal():
    """内部停止（统一复位运行态，含表情模式状态）。

    保留这个名字：`api/main.py` 的关闭流程与服务关闭路径都在用它。
    """
    MANAGER.stop_session(requester=None, reason="internal_stop")


async def shutdown_websockets():
    """服务关闭时主动断开全部活跃 WebSocket（防止 uvicorn 无限等待优雅关闭）。"""
    for ws in list(_active_ws):
        try:
            await ws.close(code=1001, reason="server shutdown")
        except Exception:
            pass
    _active_ws.clear()


# ==================== REST：视频输入能力发现 ====================

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


# ==================== WS：本机摄像头二进制帧 ====================

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
            # 只接受「本机摄像头」会话归属者的帧（归属判定在会话管理里，
            # 与 Go 边缘的 internal 路径共用同一段逻辑）
            MANAGER.inject_client_frame_bytes(username, frame_bytes)
    except WebSocketDisconnect:
        pass
    finally:
        _active_ws.discard(websocket)
        print("[WS] 本机摄像头二进制流已断开")


# ==================== WS：视频流 ====================

@router.websocket("/ws/stream")
async def video_stream(websocket: WebSocket):
    await websocket.accept()
    print("[WS] 客户端已连接")
    # ⚠ 必须登记进 _active_ws：shutdown 时靠它主动断开所有 WS，
    # 否则 uvicorn 会一直等浏览器优雅关闭（只能靠 timeout_graceful_shutdown=5 兜底）。
    _active_ws.add(websocket)

    viewer = MANAGER.attach_viewer("legacy", _ws_username(websocket), conn=websocket)
    cursor = viewer.cursor
    last_sent = -1            # 本连接已推送的最大帧号（-1 = 还没收到过帧）
    running_gen = -1          # 已发过"视频源已开始输出画面"的那一代
    push_count = 0
    push_log_time = _time.time()

    try:
        while True:
            raw = None
            try:
                raw = await asyncio.wait_for(websocket.receive_text(),
                                             timeout=WS_RECEIVE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                pass

            if raw:
                try:
                    msg = json.loads(raw)
                except Exception:
                    msg = None
                if isinstance(msg, dict):
                    # 可能涉及文件系统 stat / 打开源收尾 / DB 统计，丢线程池
                    outcome: ActionOutcome = await asyncio.to_thread(
                        MANAGER.handle_action, viewer, msg)
                    for text in outcome.replies:
                        await websocket.send_text(text)
                    # "视频流已停止"只发给收到过帧的发起人（判据在复位之前求值）
                    if outcome.framed_tail and last_sent >= 0:
                        await websocket.send_text(outcome.framed_tail)
                    if outcome.clear:
                        last_sent = -1

            sent_frame = False
            for ev in _drain(cursor):
                if isinstance(ev, ControlEvent):
                    if ev.except_viewer == viewer.id:
                        continue                     # 发起人自己已从回执里拿到
                    for text in ev.texts:
                        await websocket.send_text(text)
                    if ev.framed_tail and last_sent >= 0:
                        await websocket.send_text(ev.framed_tail)
                    if ev.clear:
                        last_sent = -1
                        running_gen = -1             # 换代/停止后重发"已在运行"
                    continue

                # 帧：落后多帧时只发最新的那一帧（丢旧留新，与 Go 侧容量 1 的语义一致）
                if ev.gen != running_gen or last_sent < 0:
                    running_gen = ev.gen
                    for text in MANAGER.viewer_sync(viewer, ev.frame_id)["replies"]:
                        await websocket.send_text(text)
                if should_send_metadata(ev.frame_id):
                    await websocket.send_text(ev.meta_text)
                try:
                    await websocket.send_bytes(frame_packet(ev.frame_id, ev.jpeg))
                except Exception as e:
                    print(f"[WS] 二进制帧发送失败: {e}")
                last_sent = ev.frame_id
                sent_frame = True
                push_count += 1

            now = _time.time()
            if push_count and now - push_log_time >= WS_FPS_LOG_INTERVAL:
                push_fps = push_count / (now - push_log_time)
                print(f"[WS] 推送FPS={push_fps:.1f} frame_id={last_sent}")
                push_log_time = now
                push_count = 0

            await asyncio.sleep(WS_SEND_INTERVAL_SECONDS if sent_frame
                                else WS_POLL_SLEEP_SECONDS)

    except WebSocketDisconnect:
        print("[WS] 客户端断开连接")
    except Exception as e:
        print(f"[WS] 错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        _active_ws.discard(websocket)
        # 最后一个观看者离开才停会话（迁移前：任一断开即停全局会话）
        MANAGER.detach_viewer(viewer.id)
        print("[WS] 连接关闭")


def _drain(cursor) -> list:
    """把游标里的事件一次性取空；帧只保留最新的一条（控制事件全保留、按序）。"""
    events = []
    while True:
        ev = cursor.next(0.0)
        if ev is None:
            break
        events.append(ev)
    frames = [e for e in events if isinstance(e, FrameEvent)]
    if len(frames) > 1:
        keep = frames[-1].seq
        events = [e for e in events
                  if not isinstance(e, FrameEvent) or e.seq == keep]
    return events
