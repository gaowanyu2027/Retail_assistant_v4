"""
热度 vs 销量比对分析 API

- GET  /api/analytics/hot-vs-sales        区域热度与销量比对（转化率 + 四象限诊断）
- POST /api/analytics/sales-records       录入/更新区域销量（POS 接入或人工录入）
- POST /api/analytics/sales-simulate      生成演示销量数据（体现四象限业务场景）
"""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class SalesRecordRequest(BaseModel):
    """单条区域销量录入"""
    zone_id: str = Field(..., description="区域ID，如 shelf_A")
    sold_count: int = Field(..., ge=0, description="售出件数")
    sales_amount: float = Field(0, ge=0, description="销售额（元）")
    period_key: str | None = Field(None, description="时段标识，缺省取当前分钟")


class SalesBatchRequest(BaseModel):
    """批量录入（多区域同一时段）"""
    period_key: str | None = None
    records: list[dict] = Field(..., description="[{zone_id, sold_count, sales_amount}]")


@router.get("/analytics/hot-vs-sales")
async def hot_vs_sales(period_key: str | None = None, hours: int = 1):
    """区域热度 vs 销量比对：转化率、四象限诊断、归因结论。"""
    try:
        from agents.sales_analytics import compare_hotness_vs_sales
        result = await asyncio.to_thread(compare_hotness_vs_sales, period_key, int(hours))
        result["source"] = "hot_vs_sales"
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"热度销量比对失败: {str(e)}")


@router.post("/analytics/sales-records")
async def upsert_sales_record(req: SalesRecordRequest):
    """录入/更新一条区域销量。"""
    try:
        import mysql_db
        from datetime import datetime
        period_key = req.period_key or datetime.now().strftime("%Y%m%d%H%M")
        await asyncio.to_thread(
            mysql_db.save_product_sales,
            req.zone_id, period_key, req.sold_count, req.sales_amount,
        )
        return {"code": 0, "msg": f"已录入 {req.zone_id} 销量 {req.sold_count} 件", "period_key": period_key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"销量录入失败: {str(e)}")


@router.post("/analytics/sales-records/batch")
async def upsert_sales_batch(req: SalesBatchRequest):
    """批量录入多个区域销量（同一时段）。"""
    try:
        import mysql_db
        from datetime import datetime
        period_key = req.period_key or datetime.now().strftime("%Y%m%d%H%M")
        for r in req.records:
            await asyncio.to_thread(
                mysql_db.save_product_sales,
                r.get("zone_id", ""), period_key,
                r.get("sold_count", 0), r.get("sales_amount", 0),
            )
        return {"code": 0, "msg": f"已录入 {len(req.records)} 条销量", "period_key": period_key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量录入失败: {str(e)}")


@router.post("/analytics/sales-simulate")
async def simulate_sales():
    """生成演示销量数据（体现 高热度低销量 / 低热度高销量 / 健康 三象限）。"""
    try:
        from agents.sales_analytics import simulate_demo_sales
        n = await asyncio.to_thread(simulate_demo_sales)
        return {"code": 0, "msg": f"已生成 {n} 条演示销量数据", "note": "演示数据，仅供功能展示"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"演示数据生成失败: {str(e)}")


@router.get("/analytics/movement-paths")
async def movement_paths(source: str | None = None, limit: int = 2000, top: int = 10):
    """购物动线分析：顾客访问序列的 A→B 关联（置信度/支持度排名 + 每区域下一站）。

    source: video=真实采集（默认全部）/ simulated=测试模拟数据
    """
    try:
        from agents.movement_analytics import analyze_movement_paths
        result = await asyncio.to_thread(analyze_movement_paths, source, int(limit), int(top))
        result["source"] = "movement_paths"
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"动线分析失败: {str(e)}")


@router.post("/analytics/paths-simulate")
async def simulate_paths(count: int = 200):
    """生成测试用模拟动线数据（source=simulated，与真实采集区分）。

    用于测试动线分析链路；真实数据由视频管线自动落库（source=video）。
    """
    try:
        from agents.movement_analytics import seed_simulated_paths
        n = await asyncio.to_thread(seed_simulated_paths, int(count))
        return {"code": 0, "msg": f"已生成 {n} 条模拟动线数据", "note": "测试数据，来源标记为 simulated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模拟动线生成失败: {str(e)}")


@router.get("/analytics/hourly-traffic")
async def hourly_traffic(hours: int = 24):
    """时段客流分析：按小时聚合客流（高峰/低谷曲线），排班/补货决策。"""
    try:
        from agents.traffic_analytics import hourly_traffic
        result = await asyncio.to_thread(hourly_traffic, int(hours))
        result["source"] = "hourly_traffic"
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"时段客流分析失败: {str(e)}")


@router.get("/analytics/hourly-alerts")
async def hourly_alerts(hours: int = 24):
    """告警时段分布：按小时聚合告警数（安保重点时段）。"""
    try:
        from agents.traffic_analytics import hourly_alerts
        result = await asyncio.to_thread(hourly_alerts, int(hours))
        result["source"] = "hourly_alerts"
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"告警时段分析失败: {str(e)}")


@router.get("/analytics/zone-depth")
async def zone_depth(hours: int = 1):
    """深度兴趣 vs 快速路过：区域选购浓度（深度占比）× 销量 四象限。"""
    try:
        from agents.traffic_analytics import zone_depth
        result = await asyncio.to_thread(zone_depth, int(hours))
        result["source"] = "zone_depth"
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"深度兴趣分析失败: {str(e)}")


@router.post("/analytics/traffic-simulate")
async def simulate_traffic():
    """生成测试用时段客流+深度模拟数据（24 小时曲线，高峰/低谷 + 三维深度场景）。"""
    try:
        from agents.traffic_analytics import seed_traffic_demo
        n = await asyncio.to_thread(seed_traffic_demo)
        return {"code": 0, "msg": f"已生成 {n} 条时段客流模拟数据", "note": "测试数据"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"时段模拟数据生成失败: {str(e)}")
