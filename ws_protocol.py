"""WS 视频流协议契约 —— **唯一定义处**（Go 混合架构迁移 · S0 冻结）。

为什么单独成模块：
    协议原先只存在于 api/routes/stream.py 的实现细节里（4 字节大端帧头、每 5 帧一次
    元数据、source_status 结构、本机摄像头拒绝文案）。数据面迁 Go（S3）后这些字节
    将由 Go 边缘侧生成并直发浏览器 —— 两侧必须**逐字一致**，因此把定义抽到这里：
    Python 侧引用它，Go 侧以 contracts/ws_messages.json 为 golden 比对。

依赖约束（有意为之，勿加）：
    只允许依赖标准库 + config.settings。不 import cv2/fastapi/numpy、不碰线程状态 ——
    CI 单测 job 只装最小依赖集，本模块必须在那里可导入（tests/test_ws_protocol_fixtures.py
    有一条子进程用例把这个约束钉住）。
"""
import json

from config.settings import WS_METADATA_INTERVAL

# 帧包头部：4 字节**大端** frame_id，随后是**原始 JPEG 字节**（无 base64）。
# 对应迁移前 stream.py 的 base64 往返（编码后存全局 → 读取时解码再拼接）。
# 去往返只改内部存储形态，**下发字节逐字不变** ——
# 见 contracts/ws_messages.json 与 tests/test_ws_protocol_fixtures.py 的字节等价用例。
FRAME_HEADER_BYTES = 4

# 「本机摄像头」被占用时的拒绝文案：前端直接展示，属于协议面，改动即破坏体验一致性
# （与 stream.py 的启动仲裁逻辑配套；单进程全局会话下的显式拒绝而非静默串台）。
CLIENT_CAMERA_BUSY_MESSAGE = (
    "已有其他用户正在使用「本机摄像头」模式，请稍后重试；"
    "多人同时观看请使用「服务器摄像头」。"
    "（若那是你之前失效的连接，刷新/关闭那个页面即可释放）"
)


def frame_packet(frame_id: int, jpeg: bytes) -> bytes:
    """组装一帧二进制消息：[4B 大端 frame_id][JPEG 原始字节]。"""
    return int(frame_id).to_bytes(FRAME_HEADER_BYTES, "big") + jpeg


def dumps_message(payload: dict) -> str:
    """把消息对象序列化成**下发的 WS 文本**。

    参数与 starlette 的 `send_json` 一致（紧凑分隔符 + 不转义非 ASCII）——
    数据面迁 Go 后文本由 Python 组装、Go 逐字节转发，两侧必须字节相同。
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def pong_message(ts) -> dict:
    """心跳应答（前端 15s 一发、8s 无 pong 即主动重连，格式属协议面）。"""
    return {"type": "pong", "ts": ts}


def should_send_metadata(frame_id: int, interval: int | None = None) -> bool:
    """元数据节拍：第 1 帧必发（首帧="源真的出画面了"的唯一可信证据），此后每 interval 帧一次。"""
    if interval is None:
        interval = WS_METADATA_INTERVAL
    return frame_id == 1 or frame_id % interval == 0


def frame_metadata_message(result: dict, mode: str) -> dict:
    """由处理线程的 _latest_result 构造帧元数据 JSON（随二进制帧择机下发的伴生消息）。"""
    msg = {
        "type": "frame",
        "frame_id": result["frame_id"],
        "timestamp": result.get("timestamp", 0),
        "tracks": result.get("tracks", []),
        "events": result.get("events", []),
        "active_suspicious": result.get("active_suspicious", []),
        "mode": mode,
    }
    # 表情模式下 result 才带 faces 键（零售模式的 result 里没有）
    if "faces" in result:
        msg["faces"] = result["faces"]
    return msg


def source_status_message(state: str, echo: dict | None = None,
                          code: str | None = None, message: str | None = None,
                          **extra) -> dict:
    """统一状态回执 `source_status` 的载荷构造（与旧 {"type":"status"} 并存，迁移期零破坏）。"""
    payload = {"type": "source_status", "state": state, "source": echo or {}}
    if code:
        payload["code"] = code
    if message:
        payload["message"] = message
    payload.update(extra)
    return payload
