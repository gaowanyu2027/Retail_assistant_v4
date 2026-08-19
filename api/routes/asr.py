"""
本地流式中文转文字（临时麦克风测试）
使用 all_models 下的 sherpa-onnx streaming zipformer 模型。
"""
import threading
import time

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config.settings import (
    ASR_MODEL_DIR,
    ASR_SAMPLE_RATE,
    ASR_ENERGY_THRESHOLD,
    ASR_TAIL_PADDING_SECONDS,
    ASR_MAX_SEGMENT_SECONDS,
    ASR_TARGET_RMS,
    ASR_MAX_GAIN,
    SHERPA_ONNX_PROVIDER,
)

router = APIRouter(prefix="/asr", tags=["asr"])
_recognizer = None
_recognizer_lock = threading.Lock()


def _get_recognizer():
    global _recognizer
    if _recognizer is None:
        import sherpa_onnx

        model_dir = ASR_MODEL_DIR
        _recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / "encoder-epoch-99-avg-1.int8.onnx"),
            decoder=str(model_dir / "decoder-epoch-99-avg-1.int8.onnx"),
            joiner=str(model_dir / "joiner-epoch-99-avg-1.int8.onnx"),
            num_threads=2,
            decoding_method="greedy_search",
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=0.5,
            rule3_min_utterance_length=20.0,
            provider=SHERPA_ONNX_PROVIDER,
        )
    return _recognizer


def _decode_ready(recognizer, stream):
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))


def _auto_gain(samples: np.ndarray):
    rms = _rms(samples)
    if rms <= 1e-4:
        return samples, 1.0
    gain = min(ASR_MAX_GAIN, ASR_TARGET_RMS / rms)
    if gain <= 1.0:
        return samples, 1.0
    return samples * gain, gain


@router.websocket("/ws")
async def asr_stream(websocket: WebSocket):
    """接收浏览器 16kHz Float32 PCM，返回本地识别文本。"""
    await websocket.accept()
    recognizer = _get_recognizer()
    stream = recognizer.create_stream()
    await websocket.send_json({"event": "ready"})

    in_speech = False
    silence_samples = 0
    speech_samples = 0
    last_interim = ""
    last_debug_at = 0.0
    segment_no = 0
    print("[ASR] 本地转文字连接已就绪")

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
            samples, gain = _auto_gain(samples)
            rms = _rms(samples)

            pending_interim = None
            pending_final = None
            with _recognizer_lock:
                stream.accept_waveform(ASR_SAMPLE_RATE, samples)
                _decode_ready(recognizer, stream)

                interim = recognizer.get_result(stream).strip()
                if interim and interim != last_interim:
                    last_interim = interim
                    pending_interim = interim

                if recognizer.is_endpoint(stream):
                    text = recognizer.get_result(stream).strip()
                    recognizer.reset(stream)
                    segment_no += 1
                    print(f"[ASR] 端点 {segment_no}，结果: {text or '无'}")
                    in_speech = False
                    silence_samples = 0
                    speech_samples = 0
                    last_interim = ""
                    if text:
                        pending_final = text

                if rms >= ASR_ENERGY_THRESHOLD:
                    in_speech = True
                    silence_samples = 0
                    speech_samples += samples.size
                else:
                    silence_samples += samples.size
                    tail_len = int(ASR_SAMPLE_RATE * ASR_TAIL_PADDING_SECONDS)
                    max_len = int(ASR_SAMPLE_RATE * ASR_MAX_SEGMENT_SECONDS)

                    if in_speech and speech_samples >= max_len:
                        stream.accept_waveform(
                            ASR_SAMPLE_RATE,
                            np.zeros(tail_len, dtype=np.float32),
                        )
                        stream.input_finished()
                        _decode_ready(recognizer, stream)
                        text = recognizer.get_result(stream).strip()
                        stream = recognizer.create_stream()
                        segment_no += 1
                        print(
                            f"[ASR] 段 {segment_no} 判定完成，"
                            f"语音 {speech_samples / ASR_SAMPLE_RATE:.2f}s，"
                            f"结果: {text or '无'}"
                        )
                        in_speech = False
                        silence_samples = 0
                        speech_samples = 0
                        last_interim = ""
                        if text:
                            pending_final = text

            if pending_interim:
                await websocket.send_json({"event": "interim", "text": pending_interim})
            if pending_final:
                await websocket.send_json({"event": "final", "text": pending_final})

            now = time.monotonic()
            if now - last_debug_at >= 0.5:
                await websocket.send_json({
                    "event": "debug",
                    "rms": round(rms, 4),
                    "gain": round(gain, 2),
                    "samples": int(samples.size),
                    "in_speech": in_speech,
                    "silence_ms": int(silence_samples * 1000 / ASR_SAMPLE_RATE),
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
