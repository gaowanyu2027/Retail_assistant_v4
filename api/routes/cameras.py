"""
摄像头管理接口 — 标识/添加摄像头 + 按需配置模块（前端"显性按钮"入口）

提供 REST 端点：
- GET  /api/cameras                   列出所有摄像头（含已装模块 + 候选池）
- GET  /api/cameras/scan              扫描/识别可用摄像头
- GET  /api/cameras/modules           列出所有已注册模块 + 候选类型
- POST /api/cameras                   添加摄像头（name/type/source/modules）
- PUT  /api/cameras/{id}              编辑摄像头
- DELETE /api/cameras/{id}            删除摄像头
- POST /api/cameras/{id}/modules      运行时加载模块（校验标签候选池）
- DELETE /api/cameras/{id}/modules/{mod}  卸载模块
- PUT  /api/cameras/{id}/modules/{mod}/enabled  开关模块
- GET  /api/cameras/{id}/modules/{mod}/stats    查询某模块统计（Agent 工具同源）

所有操作走 ModuleRegistry，运行时生效（改配置持久化，不重启、不改代码）。
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agents.module_registry import get_registry
from agents.analytics_module import candidates_for_type
from api.security import require_perm

router = APIRouter(prefix="/cameras", tags=["cameras"])

# ⚠ 权限门禁（B2 修复）：本文件里所有**改状态**的端点都要求 system:manage。
# 此前全项目只用到了 user:manage，PERMISSIONS 里声明的 data:write / system:manage
# **从未被任何端点校验** —— 于是 platform 角色（只应有 data:read/data:write）
# 可注册任意视频源摄像头、绑定输出、增删模块，属越权。
# 读接口（list / scan / modules / stats）不加门禁，保持 data:read。


class CameraCreate(BaseModel):
    name: str = Field(..., description="摄像头名称")
    type: str = Field(..., description="标签: indoor_shelf / entrance / checkout")
    source: str = Field("webcam", description="视频源：webcam / rtsp:// / file")
    modules: list[str] = Field(default_factory=list, description="实际加载的模块列表")


class ModuleAdd(BaseModel):
    module: str = Field(..., description="模块名（shelf_heat/anomaly_detect/emotion_experience）")


class ModuleEnabled(BaseModel):
    enabled: bool = Field(..., description="开关")


def _reg():
    return get_registry()


@router.get("")
async def list_cameras():
    """列出所有摄像头（含已加载模块 + 标签候选池）。"""
    return {"cameras": _reg().list_cameras()}


@router.get("/scan")
async def scan_cameras():
    """识别/扫描可用摄像头（阶段一返回配置列表，阶段二实际探测）。"""
    return {"cameras": _reg().scan_cameras()}


@router.get("/modules")
async def list_modules():
    """列出所有已注册模块 + 候选类型。"""
    return {
        "modules": _reg().list_available_modules(),
        "type_candidates": {
            t: candidates_for_type(t) for t in ("indoor_shelf", "entrance", "checkout")
        },
    }


@router.post("")
async def add_camera(req: CameraCreate,
                          _: dict = Depends(require_perm("system:manage"))):
    """添加一个摄像头（写入运行时注册表，需在候选池内校验）。"""
    reg = _reg()
    from agents.analytics_module import candidates_for_type as _c, list_registered_modules
    _TYPE_VALID = ("indoor_shelf", "entrance", "checkout")
    if req.type not in _TYPE_VALID:
        raise HTTPException(status_code=422, detail=f"未知摄像头类型: {req.type}（可选: {list(_TYPE_VALID)}）")
    # 台账 B1 收口：`source` 以前**完全不校验**，服务端会拿它去 cv2.VideoCapture() ——
    # 等于让调用方把服务端当跳板（内网端口探测 / 云元数据 / UNC 外带 NTLM / 任意文件）。
    # 这里统一过 source_guard：协议白名单 + 环回与链路本地永久封禁 + 私网可配 + 拒绝 UNC。
    try:
        import video_sources
        source = video_sources.guard_source(req.source, client_supplied=False)
    except Exception as e:
        code = getattr(e, "code", "bad_request")
        raise HTTPException(status_code=422, detail=f"视频源不被允许[{code}]: {e}")
    valid = [m for m in req.modules if m in _c(req.type) and m in list_registered_modules()]
    invalid = [m for m in req.modules if m not in valid]
    # 构造并挂到注册表（新增摄像头）—— id 用最大数字后缀递增，避免删除后碰撞覆盖
    import re as _re
    max_n = 0
    for c in reg.list_cameras():
        m = _re.search(r"cam_(\d+)$", c["id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    cam_id = "cam_" + str(max_n + 1).zfill(2)
    from agents.module_registry import Camera
    new_cam = Camera(cam_id, req.name, req.type, source, valid)
    reg._cameras[cam_id] = new_cam
    return {
        "ok": True, "camera_id": cam_id,
        "ignored_invalid_modules": invalid, "camera": new_cam.to_dict(),
    }


@router.delete("/{cam_id}")
async def delete_camera(cam_id: str,
                          _: dict = Depends(require_perm("system:manage"))):
    reg = _reg()
    if reg.get_camera(cam_id) is None:
        raise HTTPException(status_code=404, detail=f"摄像头 {cam_id} 不存在")
    reg._cameras.pop(cam_id, None)
    return {"ok": True, "removed": cam_id}


@router.post("/{cam_id}/modules")
async def add_module(cam_id: str, req: ModuleAdd,
                          _: dict = Depends(require_perm("system:manage"))):
    """运行时给某摄像头加载一个模块（校验标签候选池），并持久化到 cameras.yaml。"""
    res = _reg().add_module(cam_id, req.module)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "加载失败"))
    _reg().save_to_yaml()  # 持久化，重启不丢
    return res


@router.delete("/{cam_id}/modules/{mod}")
async def remove_module(cam_id: str, mod: str,
                          _: dict = Depends(require_perm("system:manage"))):
    res = _reg().remove_module(cam_id, mod)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "卸载失败"))
    _reg().save_to_yaml()
    return res


@router.put("/{cam_id}/modules/{mod}/enabled")
async def set_module_enabled(cam_id: str, mod: str, req: ModuleEnabled,
                          _: dict = Depends(require_perm("system:manage"))):
    res = _reg().set_module_enabled(cam_id, mod, req.enabled)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "设置失败"))
    _reg().save_to_yaml()
    return res


@router.get("/{cam_id}/modules/{mod}/stats")
async def module_stats(cam_id: str, mod: str):
    """查询某摄像头某模块统计（Agent 工具同源，供前端看板/调试）。"""
    res = _reg().get_module_stats(cam_id, mod)
    return res


class ActiveBody(BaseModel):
    cam_id: str = Field(..., description="要绑定的摄像头ID")


@router.post("/active")
async def set_active(req: ActiveBody,
                          _: dict = Depends(require_perm("system:manage"))):
    """把当前视频输出绑定到某逻辑摄像头（多路视频/切换时用）。"""
    if _reg().get_camera(req.cam_id) is None:
        raise HTTPException(status_code=404, detail=f"摄像头 {req.cam_id} 不存在")
    _reg().set_active_camera(req.cam_id)
    return {"ok": True, "active_camera": req.cam_id}
