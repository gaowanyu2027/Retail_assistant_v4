"""
热度 vs 销量比对分析 API

- GET  /api/analytics/hot-vs-sales        区域热度与销量比对（转化率 + 四象限诊断）
- GET  /api/analytics/period-compare      同期对比（当前 vs 昨天/上周同期）
- POST /api/analytics/sales/import        导入真实销量（POS/人工，整点口径）
- POST /api/analytics/sales/scan-inbox    立即扫描销量投递目录（自动同步）
- POST /api/analytics/sales-records       录入/更新区域销量（POS 接入或人工录入）
- POST /api/analytics/sales-simulate      生成演示销量数据（体现四象限业务场景）
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.security import require_perm

router = APIRouter()

# ⚠ 权限门禁（B2 修复）：本文件的**写接口**（销量录入/导入/扫描/演示数据生成）
# 统一要求 data:write。
# 动机：这些接口能直接**写入/伪造业务数据**，而之前 PERMISSIONS 里声明的
# data:write 从未被任何端点校验（全项目只用过 user:manage），
# 于是任何登录账号都能篡改用于经营决策的销量数据。
# 读取类接口（hourly-traffic / zone-depth / period-compare 等）不加门禁。


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


@router.get("/analytics/period-compare")
async def period_compare():
    """同期对比：当前时段 vs 昨天同期 / 上周同期（到访、停留、销量、销售额）。"""
    try:
        from agents.period_compare import compare_periods
        return await asyncio.to_thread(compare_periods)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同期对比失败: {str(e)}")


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


class SalesImportRequest(BaseModel):
    """真实销量批量导入（POS 导出 / 人工盘点）"""
    period_key: str | None = Field(
        None, description="时段标识（YYYYMMDDHH）；缺省取当前整点。不得使用 demo* 命名空间")
    records: list[dict] | None = Field(
        None, description="[{zone_id, sold_count, sales_amount}]，与 csv 二选一")
    csv: str | None = Field(
        None, description="CSV 文本：zone_id,sold_count,sales_amount（可带表头）")


@router.post("/analytics/sales/import")
async def import_sales(req: SalesImportRequest,
                _: dict = Depends(require_perm("data:write"))):
    """导入**真实**销量（POS / 人工录入）。

    与 /analytics/sales-simulate 的区别：本接口写入整点时段标识（YYYYMMDDHH），
    会被判定为真实数据（source=pos），可参与同期对比；
    演示数据固定使用 demo* 命名空间，归因结果会明确标注「演示数据」。
    """
    try:
        from agents.sales_ingest import import_sales as _import, parse_sales_csv

        if req.csv and req.records:
            raise HTTPException(status_code=400, detail="records 与 csv 只能提供一个")
        if req.csv:
            try:
                records = parse_sales_csv(req.csv)
            except ValueError as ve:
                raise HTTPException(status_code=400, detail=f"CSV 解析失败: {ve}")
        elif req.records:
            records = req.records
        else:
            raise HTTPException(status_code=400, detail="需提供 records 或 csv")

        try:
            return await asyncio.to_thread(_import, records, req.period_key)
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"销量导入失败: {str(e)}")


@router.post("/analytics/sales/scan-inbox")
async def scan_sales_inbox(_: dict = Depends(require_perm("data:write"))):
    """立即扫描销量投递目录（POST POS/ERP 导出的 CSV 到该目录后调用）。

    与后台定时扫描是同一逻辑：解析 → 导入（来源标记 pos）→ 归档；
    失败文件移到 failed/ 且不影响其他文件。
    """
    try:
        from agents.sales_inbox import inbox_dir, scan_once
        result = await asyncio.to_thread(scan_once)
        result["inbox_dir"] = str(inbox_dir())
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"扫描投递目录失败: {str(e)}")


@router.post("/analytics/sales-records")
async def upsert_sales_record(req: SalesRecordRequest,
                _: dict = Depends(require_perm("data:write"))):
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
async def upsert_sales_batch(req: SalesBatchRequest,
                _: dict = Depends(require_perm("data:write"))):
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
async def simulate_sales(_: dict = Depends(require_perm("data:write"))):
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
async def simulate_paths(count: int = 200,
                _: dict = Depends(require_perm("data:write"))):
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
async def simulate_traffic(_: dict = Depends(require_perm("data:write"))):
    """生成测试用时段客流+深度模拟数据（24 小时曲线，高峰/低谷 + 三维深度场景）。"""
    try:
        from agents.traffic_analytics import seed_traffic_demo
        n = await asyncio.to_thread(seed_traffic_demo)
        return {"code": 0, "msg": f"已生成 {n} 条时段客流模拟数据", "note": "测试数据"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"时段模拟数据生成失败: {str(e)}")
