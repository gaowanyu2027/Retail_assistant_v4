"""
门店出入口摄像头 — 表情分析 REST API（资源化）

资源模型：
- 摄像头运行会话：POST /emotion-cameras（启动）、DELETE /emotion-cameras（停止）、GET /emotion-cameras（状态）
- 表情记录：GET /emotion-records（最近 N 条）、GET /emotion-records/statistics（时间段统计）、
  GET /emotion-records/summary（近 N 小时汇总）

旧动作路径（/emotion_camera/*）保留为兼容别名。

⚠ 性能约定：本文件路由都是 `async def`，因此**不能**直接调用同步的 DB / 视频函数——
会在事件循环线程上阻塞，拖死同时刻的视频帧推送与 SSE 流。
其中 `stop_emotion_camera()` 尤其重（两次 emotion_record 全表 GROUP BY + 批量写 + 持锁）。
项目其它路由均已统一用 `await asyncio.to_thread(...)`，本文件此前遗漏，现已对齐。
"""
import asyncio
import time
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends

from database import (
    init_db, get_statistic, get_latest_records,
    get_record_count, cleanup_old_records,
)
from api.routes.stream import get_emotion_camera_status, stop_emotion_camera
from api.security import require_perm

router = APIRouter(tags=["emotion_camera"])


# ==================== 摄像头资源（运行会话） ====================

@router.post("/emotion-cameras")
async def start_emotion_camera(_: dict = Depends(require_perm("system:manage"))):
    """启动门店出入口摄像头（标记开始时间，实际视频流通过WebSocket控制）"""
    status = get_emotion_camera_status()
    if status["running"]:
        return {"code": 1, "msg": "门店出入口摄像头已在运行", "running": True}
    # 初始化数据库（实际视频流由前端通过 WebSocket 启动）
    await asyncio.to_thread(init_db)
    return {"code": 0, "msg": "门店出入口摄像头已就绪，请通过画面区域启动视频流", "running": False}


@router.delete("/emotion-cameras")
async def stop_emotion_camera_api(_: dict = Depends(require_perm("system:manage"))):
    """停止门店出入口摄像头，返回前后半段表情对比分析"""
    # 内部是两次全表 GROUP BY + 批量写 + 持锁，必须放线程池
    result = await asyncio.to_thread(stop_emotion_camera)
    return result


@router.get("/emotion-cameras")
async def emotion_camera_status():
    """查询门店出入口摄像头运行状态"""
    status = get_emotion_camera_status()
    return {
        "running": status["running"],
        "status": "运行中" if status["running"] else "已关闭",
        "start_time": status["start_time"],
    }


# ==================== 表情记录资源 ====================

@router.get("/emotion-records/statistics")
async def emotion_stat(start: str, end: str, camera_id: str = None):
    """查询指定时间段表情分布"""
    all_data = {}
    if not camera_id:
        all_data["出入口摄像头"] = await asyncio.to_thread(
            get_statistic, start, end, "camera_entrance")
        all_data["本机摄像头"] = await asyncio.to_thread(
            get_statistic, start, end, "camera_local")
    else:
        all_data["指定摄像头统计"] = await asyncio.to_thread(
            get_statistic, start, end, camera_id)
    return all_data


@router.get("/emotion-records")
async def emotion_latest(camera_id: str = "camera_entrance", limit: int = 20):
    """查询最近 N 条表情识别记录"""
    records = await asyncio.to_thread(get_latest_records, camera_id, limit)
    return {
        "records": [
            {"camera_id": r[0], "time": r[1], "emotion": r[2], "conf": r[3]}
            for r in records
        ]
    }


@router.get("/emotion-records/summary")
async def emotion_summary(camera_id: str = "camera_entrance", hours: int = 1):
    """查询最近 N 小时内的记录总数与分布"""
    end = datetime.now()
    start = end - timedelta(hours=hours)
    start_str = start.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end.strftime("%Y-%m-%d %H:%M:%S")
    stats = await asyncio.to_thread(get_statistic, start_str, end_str, camera_id)
    total = sum(count for _, count in stats)
    return {"total": total, "distribution": stats}


# ==================== 兼容别名（旧动作路径，deprecated） ====================
#
# ⚠ 权限门禁必须**在别名上再加一次**：别名是「直接调用」主函数的
# （`return await start_emotion_camera()`），这种调用**不经过 FastAPI 的依赖注入**，
# 所以主函数签名上的 Depends 对别名完全无效 —— 只给主函数加会被绕过。

@router.get("/emotion_camera/start", include_in_schema=False)
async def start_emotion_camera_legacy(_: dict = Depends(require_perm("system:manage"))):
    """兼容别名：GET /api/emotion_camera/start（旧路径）"""
    return await start_emotion_camera()


@router.get("/emotion_camera/stop", include_in_schema=False)
async def stop_emotion_camera_api_legacy(_: dict = Depends(require_perm("system:manage"))):
    """兼容别名：GET /api/emotion_camera/stop（旧路径）

    ⚠ 待办：这是个**改状态的 GET**（会真的停掉管线并写库）。Cookie 为 SameSite=Lax，
    顶层 GET 导航会携带 Cookie，因此存在 CSRF 面（诱导已登录管理员打开该 URL 即可停止分析）。
    应改为 POST/DELETE（`DELETE /api/emotion-cameras` 已存在），或校验 Origin。
    """
    return await asyncio.to_thread(stop_emotion_camera)


@router.get("/emotion_camera/status", include_in_schema=False)
async def emotion_camera_status_legacy():
    """兼容别名：GET /api/emotion_camera/status（旧路径）"""
    return await emotion_camera_status()


@router.get("/emotion_camera/stat", include_in_schema=False)
async def emotion_stat_legacy(start: str, end: str, camera_id: str = None):
    """兼容别名：GET /api/emotion_camera/stat（旧路径）"""
    return await emotion_stat(start, end, camera_id)


@router.get("/emotion_camera/latest", include_in_schema=False)
async def emotion_latest_legacy(camera_id: str = "camera_entrance", limit: int = 20):
    """兼容别名：GET /api/emotion_camera/latest（旧路径）"""
    return await emotion_latest(camera_id, limit)


@router.get("/emotion_camera/summary", include_in_schema=False)
async def emotion_summary_legacy(camera_id: str = "camera_entrance", hours: int = 1):
    """兼容别名：GET /api/emotion_camera/summary（旧路径）"""
    return await emotion_summary(camera_id, hours)
