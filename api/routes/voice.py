"""
POST /api/voice/command — 语音助手 MVP

固定指令先在本地匹配，避免每次语音控制都调用 LLM；
非固定指令再交给现有 MasterAgent 回答。
"""
import re
import threading
import time

import numpy as np

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.schemas import VoiceCommandRequest
from config.settings import (
    KWS_ENERGY_THRESHOLD,
    KWS_MAX_SEGMENT_SECONDS,
    KWS_SAMPLE_RATE,
    KWS_SILENCE_SECONDS,
    KWS_TAIL_PADDING_SECONDS,
)

router = APIRouter(prefix="/voice", tags=["voice"])
_kws_spotter = None
_kws_lock = threading.Lock()
_WAKE_PREFIXES = (
    "小零", "小玲", "小凌", "小灵", "小宁", "小林", "小明", "小星",
    "小心", "小清", "小英", "小冰", "小平", "小金",
)


def _normalize(text: str) -> str:
    """去掉中英文空白和常见标点，便于指令匹配。"""
    return re.sub(
        r"[\s\u3000，。！？、,.!?；;：:，·]+",
        "",
        text,
    ).lower()


def _match(text: str, aliases: list[str]) -> bool:
    return any(alias in text for alias in aliases)


def _build_command_response(text: str, **extra) -> dict:
    return {"action": "command", "text": text, **extra}


def _get_kws_spotter():
    """懒加载本地 KWS 唤醒词模型。"""
    global _kws_spotter
    if _kws_spotter is None:
        from cv_engine.wake_word import WakeWordSpotter
        _kws_spotter = WakeWordSpotter()
    return _kws_spotter


def _decode_kws_results(spotter, stream):
    """解码到无新结果为止，命中唤醒词时返回其中文标签。"""
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
        result = spotter.get_result(stream)
        if not result:
            continue
        detected = result.rsplit("@", 1)[-1] if "@" in result else result
        spotter.reset_stream(stream)
        return detected
    return None


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))


@router.websocket("/ws")
async def voice_wake_stream(websocket: WebSocket):
    """接收浏览器 16kHz Float32 PCM，实时检测唤醒词。"""
    await websocket.accept()
    spotter = _get_kws_spotter()
    stream = spotter.create_stream()
    await websocket.send_json({"event": "ready"})
    in_speech = False
    silence_samples = 0
    speech_samples = 0
    segment_no = 0
    last_debug_at = 0.0
    print("[KWS] 唤醒检测连接已就绪")

    try:
        while True:
            data = await websocket.receive_bytes()
            if not data:
                continue

            samples = np.frombuffer(data, dtype=np.float32)
            if samples.size == 0:
                continue
            samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)
            rms = _rms(samples)

            detected = None
            with _kws_lock:
                stream.accept_waveform(KWS_SAMPLE_RATE, samples)
                detected = _decode_kws_results(spotter, stream)

                if detected is None:
                    if rms >= KWS_ENERGY_THRESHOLD:
                        in_speech = True
                        silence_samples = 0
                        speech_samples += samples.size
                    else:
                        silence_samples += samples.size
                        tail_len = int(KWS_SAMPLE_RATE * KWS_TAIL_PADDING_SECONDS)
                        silence_len = int(KWS_SAMPLE_RATE * KWS_SILENCE_SECONDS)
                        max_len = int(KWS_SAMPLE_RATE * KWS_MAX_SEGMENT_SECONDS)
                        should_flush = (
                            in_speech
                            and silence_samples >= silence_len
                        )

                        if should_flush:
                            stream.accept_waveform(
                                KWS_SAMPLE_RATE,
                                np.zeros(tail_len, dtype=np.float32),
                            )
                            stream.input_finished()
                            segment_no += 1
                            print(
                                f"[KWS] 语音段 {segment_no} 补尾后判定，"
                                f"语音 {speech_samples / KWS_SAMPLE_RATE:.2f}s，"
                                f"静音 {silence_samples / KWS_SAMPLE_RATE:.2f}s"
                            )
                            detected = _decode_kws_results(spotter, stream)
                            stream = spotter.create_stream()
                            in_speech = False
                            silence_samples = 0
                            speech_samples = 0
                        elif in_speech and speech_samples >= max_len:
                            stream.input_finished()
                            segment_no += 1
                            print(
                                f"[KWS] 语音段 {segment_no} 超时判定，"
                                f"语音 {speech_samples / KWS_SAMPLE_RATE:.2f}s"
                            )
                            detected = _decode_kws_results(spotter, stream)
                            stream = spotter.create_stream()
                            in_speech = False
                            silence_samples = 0
                            speech_samples = 0

            if detected:
                print(f"[KWS] 唤醒成功: {detected}")
                await websocket.send_json({"event": "wake", "keyword": detected})
                in_speech = False
                silence_samples = 0
                speech_samples = 0
                continue

            now = time.monotonic()
            if now - last_debug_at >= 0.5:
                await websocket.send_json({
                    "event": "debug",
                    "rms": round(rms, 4),
                    "in_speech": in_speech,
                    "silence_ms": int(silence_samples * 1000 / KWS_SAMPLE_RATE),
                })
                last_debug_at = now
    except WebSocketDisconnect:
        pass
    finally:
        if stream is not None:
            try:
                stream.input_finished()
            except Exception:
                pass


def _voice_command_impl(payload: VoiceCommandRequest):
    """处理语音文本：固定指令本地执行，其余问题交给 Agent。"""
    normalized = _normalize(payload.text)
    if not normalized:
        return {"action": "answer", "text": "我没有听清，请再说一次。"}

    command = normalized
    for wake in _WAKE_PREFIXES:
        if command.startswith(wake):
            command = command[len(wake):]
            break

    # 默认“打开摄像头”使用服务器端设备；明确说“本机/本地摄像头”才用当前浏览器设备。
    if _match(command, [
        "打开本机摄像头", "打开本地摄像头", "使用本机摄像头",
        "启动本机摄像头", "本机摄像头", "本地摄像头",
    ]):
        return _build_command_response(
            "好的，正在打开当前设备的摄像头。",
            command="start",
            source="local",
        )

    if _match(command, [
        "打开服务器摄像头", "启动服务器摄像头", "服务器摄像头",
        "打开摄像头", "开启摄像头", "启动摄像头", "开始摄像头",
        "摄像头打开", "打开监控", "打开账单", "打开帐单",
        "打开照相", "打开张丹", "打开帐号", "打开",
    ]):
        return _build_command_response(
            "好的，正在打开服务器摄像头。",
            command="start",
            source="server",
        )

    if _match(command, [
        "关闭摄像头", "停止摄像头", "停止视频", "关闭视频",
        "停止监控", "关闭监控", "停止",
    ]):
        return _build_command_response(
            "好的，正在停止视频。",
            command="stop",
        )

    if _match(command, ["暂停摄像头", "暂停视频", "暂停"]):
        return _build_command_response(
            "好的，视频已暂停。",
            command="pause",
        )

    if _match(command, [
        "恢复摄像头", "继续摄像头", "恢复视频", "继续视频",
        "恢复", "继续",
    ]):
        return _build_command_response(
            "好的，视频继续播放。",
            command="resume",
        )

    if _match(command, ["零售模式", "货架模式", "切到零售", "切换到零售", "切换零售"]):
        return _build_command_response(
            "好的，已切换到零售分析模式。",
            command="mode",
            mode="retail",
        )

    if _match(command, [
        "表情模式", "人脸模式", "出入口模式",
        "切到表情", "切换到表情", "切换表情",
    ]):
        return _build_command_response(
            "好的，已切换到表情分析模式。",
            command="mode",
            mode="emotion",
        )

    if _match(command, [
        "当前状态", "系统状态", "查看状态", "运行状态",
        "什么状态", "现在状态", "状态",
    ]):
        from api.routes.stream import get_stream_status
        status = get_stream_status()
        mode_cn = "零售分析" if status.get("mode") == "retail" else "表情分析"
        source_cn = {
            "webcam": "服务器摄像头",
            "client": "本机摄像头",
            "file": "视频文件",
        }.get(status.get("source"), "无")
        running_cn = "运行中" if status.get("running") else "已停止"
        paused_cn = "，已暂停" if status.get("paused") else ""
        text = f"当前状态：{running_cn}{paused_cn}，模式：{mode_cn}，视频源：{source_cn}。"
        return _build_command_response(text, command="status")

    # 非固定指令：交给现有 Agent 回答。
    try:
        from api.routes.query import get_agent
        agent = get_agent(payload.session_id)
        result = agent.handle_query(
            payload.text,
            session_id=payload.session_id,
        )
        return {
            "action": "answer",
            "text": result.get("answer", "我已经完成分析。"),
            "intent": result.get("intent", "general"),
            "data": result.get("data", {}),
            "suggestions": result.get("suggestions", []),
        }
    except Exception:
        return {
            "action": "answer",
            "text": "语音问答暂时不可用，请稍后再试。",
        }


@router.post("/commands")
def create_voice_command(payload: VoiceCommandRequest):
    """处理语音文本（创建一条语音指令），并写入 MySQL 语音指令日志。

    RESTful：语音指令作为资源，POST /api/voice/commands 提交。
    """
    start = time.time()
    response = _voice_command_impl(payload)

    normalized = _normalize(payload.text)
    command = normalized
    wake_word = ""
    for wake in _WAKE_PREFIXES:
        if command.startswith(wake):
            wake_word = wake
            command = command[len(wake):]
            break

    try:
        import mysql_db
        mysql_db.save_voice_command_log(
            session_id=payload.session_id,
            raw_text=payload.text,
            command=command,
            action=response.get("command", "answer"),
            status="ok",
            wake_word=wake_word,
            source="voice",
            latency_ms=int((time.time() - start) * 1000),
        )
    except Exception as e:
        print(f"[MySQL] 语音指令日志写入失败: {e}")

    return response


@router.post("/command", include_in_schema=False)
def voice_command(payload: VoiceCommandRequest):
    """兼容别名：POST /api/voice/command（旧路径）→ 等价于 POST /api/voice/commands"""
    return create_voice_command(payload)
