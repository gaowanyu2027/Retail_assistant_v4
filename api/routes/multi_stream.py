"""
多路视频流管理接口 — 后台并行采集/分析，按摄像头启停

引擎：cv_engine/multi_stream.py（MultiStreamEngine），每路独立线程喂模块（数据隔离）。
"""
from fastapi import APIRouter, Depends, HTTPException

from cv_engine.multi_stream import get_engine
from agents.module_registry import get_registry
from api.security import require_perm

router = APIRouter(prefix="/streams", tags=["multi-stream"])

# ⚠ 权限门禁（B2 修复）：启停采集是**系统级**操作，要求 system:manage。
# 此前 PERMISSIONS 里声明的 data:write / system:manage **从未被任何端点校验**
# （全项目只用了 user:manage），于是 platform 角色可随意启停门店采集。
# `/status` 是只读接口，不加门禁。


@router.post("/{cam_id}/start")
async def start_stream(cam_id: str,
                       _: dict = Depends(require_perm("system:manage"))):
    """启动某摄像头的后台分析线程。"""
    reg = get_registry()
    if reg.get_camera(cam_id) is None:
        raise HTTPException(status_code=404, detail=f"摄像头 {cam_id} 不存在")
    res = get_engine().start(cam_id)
    return {"ok": True, "camera": cam_id, "started": cam_id in res["started"]}


@router.post("/start-all")
async def start_all(_: dict = Depends(require_perm("system:manage"))):
    """启动所有已配 source 的摄像头分析。"""
    res = get_engine().start()
    return res


@router.post("/{cam_id}/stop")
async def stop_stream(cam_id: str,
                      _: dict = Depends(require_perm("system:manage"))):
    res = get_engine().stop(cam_id)
    return {"ok": True, "stopped": cam_id in res["stopped"]}


@router.get("/status")
async def stream_status():
    return {"streams": get_engine().status()}
