"""
百度地图 REST 接口 — POI 竞品 / 商圈 / 地理编码 / 距离测算

与 Agent 工具（agents/map_tools.py）共用同一批函数；这里暴露为独立 REST 端点，
供 Excel 门店表转坐标 / 分布热力图前端 / 批量调度等场景直接调用（非对话式）。
"""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from agents.map_tools import (
    check_competitors,
    analyze_surrounding,
    batch_geocode,
    calc_distances,
)

router = APIRouter(prefix="/maps", tags=["maps"])


class GeocodeRequest(BaseModel):
    """地址批量转经纬度请求"""
    addresses: list[str] = Field(..., description="地址字符串列表")


class DistanceRequest(BaseModel):
    """距离测算请求：起点 + 多个目标点"""
    origin_lng: float
    origin_lat: float
    dests: list[dict] = Field(..., description="目标点列表 [{lng, lat}]")


@router.post("/geocode")
async def geocode(req: GeocodeRequest):
    """地址批量转经纬度（配 Excel 门店表 → 分布热力图）。"""
    try:
        return json.loads(await asyncio.to_thread(batch_geocode, req.addresses))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"地理编码失败: {e}")


@router.get("/competitors")
async def competitors(
    lng: float = Query(..., description="门店经度"),
    lat: float = Query(..., description="门店纬度"),
    radius: float = Query(3.0, ge=0.5, le=10, description="检索半径（公里）"),
    query: str = Query("超市", description="竞品关键词"),
):
    """POI 竞品探查：周边同类零售店数量/分布/最近距离。"""
    try:
        return json.loads(await asyncio.to_thread(check_competitors, query, lng, lat, radius))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"竞品探查失败: {e}")


@router.get("/surrounding")
async def surrounding(
    lng: float = Query(..., description="门店经度"),
    lat: float = Query(..., description="门店纬度"),
    radius: float = Query(2.0, ge=0.5, le=10, description="分析半径（公里）"),
):
    """商圈配套分析：周边小区/写字楼/学校/地铁等 POI 统计 + 客流潜力。"""
    try:
        return json.loads(await asyncio.to_thread(analyze_surrounding, lng, lat, radius))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"商圈分析失败: {e}")


@router.post("/distance")
async def distance(req: DistanceRequest):
    """距离测算：仓库/门店到多个目标点的驾车距离。"""
    try:
        return json.loads(await asyncio.to_thread(
            calc_distances, req.origin_lng, req.origin_lat, req.dests))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"距离测算失败: {e}")
