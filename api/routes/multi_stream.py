"""
多路视频流管理接口 — 后台并行采集/分析，按摄像头启停

引擎：cv_engine/multi_stream.py（MultiStreamEngine），每路独立线程喂模块（数据隔离）。
"""
from fastapi import APIRouter, HTTPException

from cv_engine.multi_stream import get_engine
from agents.module_registry import get_registry

router = APIRouter(prefix="/streams", tags=["multi-stream"])


@router.post("/{cam_id}/start")
async def start_stream(cam_id: str):
    """启动某摄像头的后台分析线程。"""
    reg = get_registry()
    if reg.get_camera(cam_id) is None:
        raise HTTPException(status_code=404, detail=f"摄像头 {cam_id} 不存在")
    res = get_engine().start(cam_id)
    return {"ok": True, "camera": cam_id, "started": cam_id in res["started"]}


@router.post("/start-all")
async def start_all():
    """启动所有已配 source 的摄像头分析。"""
    res = get_engine().start()
    return res


@router.post("/{cam_id}/stop")
async def stop_stream(cam_id: str):
    res = get_engine().stop(cam_id)
    return {"ok": True, "stopped": cam_id in res["stopped"]}


@router.get("/status")
async def stream_status():
    return {"streams": get_engine().status()}
