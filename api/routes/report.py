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
from config.settings import EDGE_REPORT, EDGE_REPORT_URL, INTERNAL_API_TOKEN

router = APIRouter(tags=["report"])

# ==================== 主动汇报 WebSocket 推送 ====================

# EDGE_REPORT=1 时订阅连接由 Go edge 持有，这里只保留本地回退：
# 直连 Python 的调试连接、以及 edge 未接管（反代）时的原有拓扑。
# 生产拓扑下这两个模块级单例是空集 / 仅用于本地投递。
_report_clients: set[WebSocket] = set()
_report_loop: asyncio.AbstractEventLoop | None = None

# 内网调用**不经过环境变量里的 HTTP 代理**（汇报必须直达 edge；代理通常
# 只对公网配置，把内网地址也丢进去只会多一跳或直接失败）。
# 懒加载：启动阶段其它线程正在导入 LLM/HTTP 依赖栈，这里若在模块导入时
# 拉入 `urllib.request`，Python 3.13 的模块锁可能撞上报
# `deadlock detected by _ModuleLock('urllib3.util')`（实测会让定时汇报
# 整个启动失败）。汇报是低频调用，第一次投递时再建 opener 没有代价。
_EDGE_OPENER = None


def _edge_opener():
    global _EDGE_OPENER
    if _EDGE_OPENER is None:
        import urllib.request
        _EDGE_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _EDGE_OPENER


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
    """向所有订阅者推送汇报（可从后台线程调用）。

    EDGE_REPORT=1 时订阅连接由 Go edge 持有：先本地投递（直连 Python 的调试
    连接 / 回退拓扑），再把同一条消息交给 edge 扇出。远程失败只记日志——
    汇报是低频的尽力而为消息，不能反过来影响定时任务。
    """
    _broadcast_local(payload)
    _broadcast_edge(payload)


def _broadcast_local(payload: dict):
    """原有拓扑：向 Python 进程内的订阅集合投递。"""
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


def _broadcast_edge(payload: dict) -> bool:
    """把一条汇报交给 edge 的内网广播入口（EDGE_REPORT=1 时启用）。

    与 Python 内部端点的门禁一致：`X-Internal-Token` 共享密钥；edge 侧还会
    校验对端必须是环回/私网/链路本地地址，公网打不到这个端点。
    """
    if not EDGE_REPORT or not EDGE_REPORT_URL or not INTERNAL_API_TOKEN:
        return False
    import json
    import urllib.request

    # 与 Starlette 的 send_json 一样用紧凑分隔符；ensure_ascii=False 让中文
    # 原样走 UTF-8（WS 文本帧本就是 UTF-8，JSON.parse 对两种写法等价）。
    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        EDGE_REPORT_URL + "/internal/report/broadcast",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": INTERNAL_API_TOKEN,
        },
    )
    try:
        with _edge_opener().open(req, timeout=3) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[ReportWS] edge 广播失败: {e}")
        return False


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


@router.get("/reports/data-quality")
async def get_data_quality():
    """数据可信度门禁结果：区分「真的没生意」与「设备/视频源异常」。

    ⚠ 顶层 `reliable` 采用**零售源（服务器摄像头）**的判定，而不是
    "任一路新鲜"的聚合口径——本接口是给仪表盘判断"业务数字能不能信"用的，
    而业务数字都来自零售源。若用聚合口径，浏览器推帧（client）会把停摆的
    服务器摄像头掩盖成"可信"，预警横幅就不会出现（见 agents/data_quality.py）。

    各源明细见 `reliable_by_source` / `sources`。
    """
    try:
        from agents import data_quality
        snap = data_quality.snapshot()
        by_src = snap.get("reliable_by_source") or {}
        if by_src:
            snap = dict(snap)
            snap["reliable"] = bool(by_src.get(data_quality.SOURCE_RETAIL, False))
            snap["reliable_scope"] = "retail"   # 说明顶层 reliable 是哪个口径
        return snap
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取数据可信度失败: {str(e)}")


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
