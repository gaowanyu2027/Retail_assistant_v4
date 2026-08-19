"""
报表数据接口（资源化）

资源模型：
- GET /reports/popularity、/reports/anomaly、/reports/dashboard、/reports/heat-reports、/reports/health
- WS /ws/reports — 主动汇报推送通道（后台汇报 Agent 广播用）

旧路径（/report/*）保留为兼容别名。
"""
import asyncio

from fastapi import APIRouter, Query, HTTPException, WebSocket, WebSocketDisconnect
from api.schemas import (
    PopularityReportResponse, AnomalyReportResponse, DashboardSnapshot,
    ZoneStatsResponse,
)

router = APIRouter(tags=["report"])

# ==================== 主动汇报 WebSocket 推送 ====================

_report_clients: set[WebSocket] = set()
_report_loop: asyncio.AbstractEventLoop | None = None


def set_report_loop(loop: asyncio.AbstractEventLoop | None):
    """由应用生命周期注入事件循环，供后台线程安全广播。"""
    global _report_loop
    _report_loop = loop


async def _report_ws_handler(websocket: WebSocket):
    """订阅主动汇报推送（保持连接，接收新汇报消息）。"""
    await websocket.accept()
    _report_clients.add(websocket)
    print("[ReportWS] 汇报订阅连接:", len(_report_clients))
    try:
        while True:
            # 保持连接；客户端断开/发心跳均在此循环处理
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _report_clients.discard(websocket)
        print("[ReportWS] 汇报订阅断开:", len(_report_clients))


@router.websocket("/ws/reports")
async def report_ws(websocket: WebSocket):
    await _report_ws_handler(websocket)


@router.websocket("/report/ws/reports")
async def report_ws_legacy(websocket: WebSocket):
    """兼容别名：旧 WS 路径 /api/report/ws/reports"""
    await _report_ws_handler(websocket)


def broadcast_report(payload: dict):
    """向所有订阅者推送汇报（可从后台线程调用）。"""
    if not _report_clients or _report_loop is None:
        return

    async def _send():
        for ws in list(_report_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                _report_clients.discard(ws)

    try:
        asyncio.run_coroutine_threadsafe(_send(), _report_loop)
    except Exception as e:
        print(f"[ReportWS] 广播失败: {e}")


# ==================== 报表资源 ====================

@router.get("/reports/popularity")
async def get_popularity_report(
    zone_id: str | None = Query(default=None, description="指定区域ID，不传则返回全部"),
):
    """获取货架热度报表（三维评分版）"""
    from api.dependencies import get_popularity_skill

    try:
        skill = get_popularity_skill()
        stats = skill.get_stats()

        if zone_id:
            zone_data = stats["zones"].get(zone_id)
            if not zone_data:
                raise HTTPException(status_code=404, detail=f"区域 {zone_id} 不存在")
            stats["zones"] = {zone_id: zone_data}

        return stats

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取热度报表失败: {str(e)}")


@router.get("/reports/anomaly", response_model=AnomalyReportResponse)
async def get_anomaly_report(
    level: str | None = Query(default=None, pattern=r"^(watch|high)$"),
    min_score: int | None = Query(default=None, ge=0, le=100),
):
    """获取异常行为报表

    快速模式：直接从Skill读取告警数据。
    """
    from api.dependencies import get_anomaly_skill

    try:
        skill = get_anomaly_skill()
        summary = skill.get_alert_summary()

        # 应用可选过滤条件
        if level is not None or min_score is not None:
            alerts = skill.get_alerts(level=level, min_score=min_score)
            high = [a for a in alerts if a.get("level") == "high"]
            watch = [a for a in alerts if a.get("level") == "watch"]
            summary = {
                "total_alerts": len(alerts),
                "high_risk_count": len(high),
                "watch_count": len(watch),
                "high_risk": high,
                "watch_list": watch,
            }

        return AnomalyReportResponse(**summary)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取异常报表失败: {str(e)}")


@router.get("/reports/dashboard", response_model=DashboardSnapshot)
async def get_dashboard():
    """获取仪表盘全局快照"""
    from api.dependencies import get_popularity_skill, get_anomaly_skill
    from agents.master_agent import build_dashboard_snapshot

    try:
        snapshot = build_dashboard_snapshot(
            get_popularity_skill(),
            get_anomaly_skill(),
        )
        return DashboardSnapshot(**snapshot)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取仪表盘数据失败: {str(e)}")


@router.get("/reports/heat-reports")
async def get_heat_reports(limit: int = Query(default=20, ge=1, le=100)):
    """获取定期生成的热度汇报。"""
    try:
        import mysql_db
        return {"reports": mysql_db.get_latest_heat_reports(limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取热度汇报失败: {str(e)}")


@router.get("/reports/health")
async def health_check():
    """健康检查"""
    import torch
    from api.dependencies import get_uptime_seconds

    return {
        "status": "ok",
        "gpu_available": torch.cuda.is_available(),
        "device": "cuda:0" if torch.cuda.is_available() else "cpu",
        "uptime_seconds": get_uptime_seconds(),
    }


# ==================== 兼容别名（旧路径，deprecated） ====================

@router.get("/report/popularity", include_in_schema=False)
async def get_popularity_report_legacy(zone_id: str | None = Query(default=None)):
    """兼容别名：GET /api/report/popularity（旧路径）"""
    return await get_popularity_report(zone_id)


@router.get("/report/anomaly", response_model=AnomalyReportResponse, include_in_schema=False)
async def get_anomaly_report_legacy(
    level: str | None = Query(default=None, pattern=r"^(watch|high)$"),
    min_score: int | None = Query(default=None, ge=0, le=100),
):
    """兼容别名：GET /api/report/anomaly（旧路径）"""
    return await get_anomaly_report(level, min_score)


@router.get("/report/dashboard", response_model=DashboardSnapshot, include_in_schema=False)
async def get_dashboard_legacy():
    """兼容别名：GET /api/report/dashboard（旧路径）"""
    return await get_dashboard()


@router.get("/report/heat-reports", include_in_schema=False)
async def get_heat_reports_legacy(limit: int = Query(default=20, ge=1, le=100)):
    """兼容别名：GET /api/report/heat-reports（旧路径）"""
    return await get_heat_reports(limit)


@router.get("/report/health", include_in_schema=False)
async def health_check_legacy():
    """兼容别名：GET /api/report/health（旧路径）"""
    return await health_check()
