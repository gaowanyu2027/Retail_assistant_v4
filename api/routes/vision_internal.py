"""视频数据面的内部端点 —— 仅供内网同伴进程（Go edge）调用，**不面向公网**。

分工（见《项目部分go语言重构》与改进记录）：视频帧的采集/推理/编码仍在 Python，
**连接与扇出在 Go**。因此这里提供四件东西：

- `GET  /internal/vision/events`      上行帧流（chunked，单向，Go 长期订阅）
- `POST /internal/vision/action`      客户端动作转发（开/停/暂停/心跳以外的一切）
- `POST /internal/vision/viewer`      观看者登记（attach 时顺带取"已在运行"问候）
- `POST /internal/vision/client-frame` 浏览器本机摄像头帧（原始 JPEG 字节体）

**上行帧流的信封**（`magic|ver|kind|frame_id|metaLen|jpegLen|meta|jpeg`，全部大端；
两侧都是标准库，`curl -N` 可直接观察）：

    kind=1 帧：meta = {"gen":G,"frame_id":F,"msg":"<帧元数据 WS 文本>"}，jpeg = 帧字节
    kind=2 控制：meta = {"gen":G,"clear":bool,"except":"<viewer_id>|null",
                        "framed_tail":"<文本>|null","texts":["<WS 文本>", ...]}，jpeg 空
    kind=3 心跳：meta/jpeg 皆空（1s 无事件发一次，供 Go 判断"Python 还在"）

文本消息**一律由 Python 组装**（含中文与任意 JSON 键序），Go 只做转发与二进制封装
（`[4B 大端 frame_id][JPEG]`），这样两侧不必各自复刻一套文案。

门禁与 `/internal/auth/*` 完全一致（`api/routes/internal.py` 的 `require_internal_token`：
共享密钥 + 内网对端，任一不满足即 404）。端点用 `def`（非 async）：帧流的游标等待会
阻塞，跑在 FastAPI 线程池里不会占住事件循环。
"""
from __future__ import annotations

from typing import Iterator

from fastapi import APIRouter, Body, Depends, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.routes.internal import require_internal_token
from vision.session import MANAGER, ControlEvent, FrameEvent
from ws_protocol import dumps_message

router = APIRouter(tags=["internal"], include_in_schema=False)

# 上行流信封常量（与 edge/internal/video 的实现成对，改动必须两侧同步）
_MAGIC = b"DSHF"
_VERSION = 1
_KIND_FRAME = 1
_KIND_CONTROL = 2
_KIND_HEARTBEAT = 3
_HEARTBEAT_SECONDS = 1.0


def _envelope(kind: int, frame_id: int, meta: bytes, jpeg: bytes) -> bytes:
    return (
        _MAGIC + bytes([_VERSION, kind])
        + int(frame_id).to_bytes(4, "big")
        + len(meta).to_bytes(4, "big")
        + len(jpeg).to_bytes(4, "big")
        + meta + jpeg
    )


# ==================== 请求模型 ====================

class ActionRequest(BaseModel):
    viewer: str = ""
    username: str | None = None
    message: dict = {}


class ViewerRequest(BaseModel):
    op: str = "attach"                  # attach | detach
    viewer: str = ""
    username: str | None = None
    frame_id: int | None = None         # attach 时带上"即将下发的帧号"（回执里要用）


# ==================== 上行帧流 ====================

def _stream_chunks() -> Iterator[bytes]:
    """长期订阅：帧/控制事件按 seq 顺序推出，闲着发心跳。

    慢读者在这里**不会**拖住发布者：事件日志的帧环只留最近几帧，追不上的旧帧已被环
    挤掉（积压上限即环容量）；真正的"丢旧留新"在 Go 侧 —— 每客户端 outbound 容量 1，
    满则丢旧留新。控制事件不丢，按 seq 与帧事件全局有序。
    """
    MANAGER.edge_stream_opened()
    cursor = MANAGER.log.cursor()
    try:
        while True:
            ev = cursor.next(timeout=_HEARTBEAT_SECONDS)
            if ev is None:
                yield _envelope(_KIND_HEARTBEAT, 0, b"", b"")
                continue
            if isinstance(ev, FrameEvent):
                meta = dumps_message({
                    "gen": ev.gen, "frame_id": ev.frame_id, "msg": ev.meta_text,
                }).encode("utf-8")
                yield _envelope(_KIND_FRAME, ev.frame_id, meta, ev.jpeg)
            else:
                meta = dumps_message({
                    "gen": ev.gen, "clear": ev.clear,
                    "except": ev.except_viewer, "framed_tail": ev.framed_tail,
                    "texts": list(ev.texts),
                }).encode("utf-8")
                yield _envelope(_KIND_CONTROL, 0, meta, b"")
    finally:
        MANAGER.edge_stream_closed()


@router.get("/vision/events", dependencies=[Depends(require_internal_token)])
def vision_events() -> StreamingResponse:
    """Go 边缘订阅的视频事件流（HTTP/1.1 chunked，单向；重连后从"当前最新帧"续起）。"""
    return StreamingResponse(_stream_chunks(), media_type="application/octet-stream",
                             headers={"Cache-Control": "no-store"})


# ==================== 动作 / 观看者 ====================

@router.post("/vision/action", dependencies=[Depends(require_internal_token)])
def vision_action(req: ActionRequest) -> dict:
    """转发一条客户端动作（消息体原样），返回发给**发起人**的回执。

    回执语义见 `vision.session.ActionOutcome`：`replies` 按序下发；
    `framed_tail` 只在"发起人已收到过帧"时补发；`clear` 表示丢弃待发帧并复位帧号。
    """
    viewer = MANAGER.attach_viewer("edge", req.username, viewer_id=req.viewer or None)
    return MANAGER.handle_action(viewer, req.message).to_json()


@router.post("/vision/viewer", dependencies=[Depends(require_internal_token)])
def vision_viewer(req: ViewerRequest) -> dict:
    """观看者登记/注销。

    attach 返回 `{"viewer": id, "gen": G, "replies": [...]}` —— 会话在跑且已有帧时，
    `replies` 是"视频源已开始输出画面"（源描述符用会话的，帧号由调用方指定）。
    """
    if req.op == "detach":
        MANAGER.detach_viewer(req.viewer or None)
        return {"gen": MANAGER.log.gen, "replies": []}
    viewer = MANAGER.attach_viewer("edge", req.username, viewer_id=req.viewer or None)
    sync = MANAGER.viewer_sync(viewer, req.frame_id)
    return {"viewer": viewer.id, **sync}


@router.post("/vision/client-frame", dependencies=[Depends(require_internal_token)])
def vision_client_frame(
    payload: bytes = Body(default=b"", media_type="application/octet-stream"),
    username: str = Query("", description="推帧的登录用户名（归属校验用）"),
) -> Response:
    """浏览器本机摄像头的一帧（原始 JPEG）。

    **归属判定留在 Python**（`SessionManager.inject_client_frame_bytes`）——
    与 /ws/client 的直连路径共用同一段逻辑，Go 侧不复刻。
    """
    MANAGER.inject_client_frame_bytes(username or None, payload)
    return Response(status_code=204)
