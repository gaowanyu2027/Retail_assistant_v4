"""服务端语音合成接口（edge-tts）"""
import asyncio
import hashlib

import edge_tts
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

router = APIRouter(prefix="/tts", tags=["tts"])

TTS_VOICE = "zh-CN-XiaoxiaoNeural"
_cache: dict[str, bytes] = {}


class TTSRequest(BaseModel):
    text: str


@router.get("")
async def synthesize(text: str = "你好"):
    """将中文文本合成为 mp3 音频。"""
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")

    cache_key = hashlib.sha1(
        (TTS_VOICE + text).encode("utf-8")
    ).hexdigest()
    if cache_key in _cache:
        return Response(
            content=_cache[cache_key],
            media_type="audio/mpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    try:
        import mysql_db
        cached_audio = await asyncio.to_thread(mysql_db.get_tts_cache, cache_key)
        if cached_audio:
            _cache[cache_key] = cached_audio
            return Response(
                content=cached_audio,
                media_type="audio/mpeg",
                headers={"Cache-Control": "public, max-age=3600"},
            )
    except Exception as e:
        print(f"[TTS] MySQL 缓存读取失败: {e}")

    try:
        communicate = edge_tts.Communicate(text, voice=TTS_VOICE)
        chunks = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                chunks.append(chunk["data"])

        audio = b"".join(chunks)
        if not audio:
            raise HTTPException(status_code=502, detail="TTS 未生成音频")

        if len(_cache) >= 128:
            _cache.clear()
        _cache[cache_key] = audio
        try:
            import mysql_db
            await asyncio.to_thread(mysql_db.save_tts_cache, cache_key, text, audio)
        except Exception as e:
            print(f"[TTS] MySQL 缓存写入失败: {e}")
        return Response(
            content=audio,
            media_type="audio/mpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"TTS 服务暂不可用: {exc}",
        ) from exc


@router.post("")
async def synthesize_post(payload: TTSRequest):
    return await synthesize(payload.text)
