"""
百度地图工具 — POI 竞品探查 / 商圈配套分析 / 地址批量转坐标 / 距离测算

接入方式：作为 Agent 工具（@tool 装饰器，与其他技能一致）注册进 master_agent，
使 LLM 能按用户意图调用；batch_geocode 同时暴露为标准 REST 接口（/api/maps/geocode）。

依赖：服务端 AK + SK（sn 签名）。AK/SK 从环境变量读取（badu_map_ak / baidu_map_sk）。
未配置 AK 时工具返回可读错误，不阻塞主流程（fail-open）。
"""
import hashlib
import json
import time
from urllib.parse import quote

import httpx

# ===== 地图查询本地 TTL 缓存（降配额 + 提速；地理编码/距离等重复查询无需再调 API） =====
_MAP_CACHE: dict[str, tuple[float, str]] = {}
_MAP_CACHE_TTL = 3600       # 缓存 1 小时（地址→坐标、固定点→距离相对稳定）
_MAP_CACHE_MAX = 500


def _cache_get(key: str) -> str | None:
    item = _MAP_CACHE.get(key)
    if item and item[0] > time.time():
        return item[1]
    _MAP_CACHE.pop(key, None)
    return None


def _cache_set(key: str, value: str, ttl: int = _MAP_CACHE_TTL) -> None:
    if len(_MAP_CACHE) >= _MAP_CACHE_MAX:
        _MAP_CACHE.clear()
    _MAP_CACHE[key] = (time.time() + ttl, value)

from config.settings import BAIDU_MAP_AK, BAIDU_MAP_SK

_BASE = "https://api.map.baidu.com"
_TIMEOUT = 15


def _sign(params: dict) -> str:
    """百度地图 sn 签名：参数按 key ASCII 升序，value 做 RFC3986 编码，
    拼接后末尾拼 SK，再取 MD5 十六进制小写。SK 未配置则返回空串（兼容仅 AK 接口）。"""
    if not BAIDU_MAP_SK:
        return ""
    qs = "&".join(
        f"{k}={quote(str(v), safe='')}" for k, v in sorted(params.items()) if k not in ("sk", "sn")
    )
    return hashlib.md5((qs + BAIDU_MAP_SK).encode("utf-8")).hexdigest()


def _request(path: str, params: dict) -> dict:
    """统一请求：补齐 ak、sn，返回百度响应 JSON；异常返回可读错误结构。"""
    if not BAIDU_MAP_AK:
        return {"status": "error", "message": "未配置百度地图 AK（环境变量 baidu_map_ak）"}
    params = dict(params)
    params["ak"] = BAIDU_MAP_AK
    params["output"] = "json"
    sn = _sign(params)
    if sn:
        params["sn"] = sn
    try:
        resp = httpx.get(f"{_BASE}{path}", params=params, timeout=_TIMEOUT)
        data = resp.json()
    except Exception as e:
        return {"status": "error", "message": f"百度地图请求失败: {e}", "path": path}
    if data.get("status") != 0:
        return {"status": "error", "message": f"百度地图返回{data.get('status')}: {data.get('message','')}", "path": path}
    return data


def _haversine(lng1, lat1, lng2, lat2) -> float:
    """两点球面距离（米）。"""
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def check_competitors(query: str, lng: float, lat: float, radius_km: float = 3.0) -> str:
    """
    POI 竞品探查：检索某坐标周边 radius_km 内与 query 匹配的零售店，统计数量与最近距离。

    参数 query: 竞品关键词（如 "超市"、"便利店"、"零食店"）
    参数 lng/lat: 门店坐标（经度/纬度）
    参数 radius_km: 检索半径（公里，默认 3）
    """
    radius_m = int(radius_km * 1000)
    data = _request("/place/v2/search", {
        "query": query,
        "location": f"{lat},{lng}",
        "radius": radius_m,
        "page_size": 25,
        "page_num": 0,
    })
    if data.get("status") != 0:
        return json.dumps(data, ensure_ascii=False)
    results = data.get("results", [])
    items = []
    for r in results:
        loc = r.get("location", {})
        name = r.get("name", "")
        addr = r.get("address", "")
        lat2, lng2 = loc.get("lat"), loc.get("lng")
        dist = round(_haversine(lng, lat, lng2, lat2)) if lat2 and lng2 else None
        items.append({"name": name, "address": addr, "distance_m": dist})
    total = data.get("total", len(items))
    nearest = min((i["distance_m"] for i in items if i["distance_m"] is not None), default=None)
    return json.dumps({
        "query": query,
        "center": {"lng": lng, "lat": lat},
        "radius_km": radius_km,
        "competitor_count": total,
        "listed_count": len(items),
        "nearest_distance_m": nearest,
        "density": "较密集" if total > 10 else ("一般" if total > 3 else "稀疏"),
        "competitors": items,
    }, ensure_ascii=False)


def analyze_surrounding(lng: float, lat: float, radius_km: float = 2.0) -> str:
    """
    商圈配套分析：统计周边小区/写字楼/学校/地铁站等 POI 数量，评估客流潜力。

    参数 lng/lat: 门店坐标
    参数 radius_km: 分析半径（公里，默认 2）
    """
    radius_m = int(radius_km * 1000)
    poi_types = [
        ("小区", "住宅小区"),
        ("写字楼", "写字楼"),
        ("学校", "学校"),
        ("地铁站", "地铁站"),
        ("商场", "购物中心"),
    ]
    out = {"center": {"lng": lng, "lat": lat}, "radius_km": radius_km, "poi": {}}
    for key, kw in poi_types:
        data = _request("/place/v2/search", {
            "query": kw, "location": f"{lat},{lng}",
            "radius": radius_m, "page_size": 20, "page_num": 0,
        })
        if data.get("status") == 0:
            out["poi"][key] = {
                "count": data.get("total", 0),
                "listed": data.get("results", [])[:5] if data.get("results") else [],
            }
        else:
            out["poi"][key] = {"count": 0, "error": data.get("message", "")}

    # 简易区位潜力度量：住宅/地铁/写字楼加权
    residential = out["poi"]["小区"]["count"]
    metro = out["poi"]["地铁站"]["count"]
    office = out["poi"]["写字楼"]["count"]
    score = min(100, residential * 8 + metro * 12 + office * 6)
    out["traffic_potential"] = {
        "score": score,
        "level": "优" if score >= 60 else ("良" if score >= 30 else "一般"),
    }
    return json.dumps(out, ensure_ascii=False)


def batch_geocode(addresses: list) -> str:
    """
    地址批量转经纬度（用于 Excel 门店表 → 分布热力图）。含节流与缓存。

    参数 addresses: 地址字符串列表
    """
    if not isinstance(addresses, list):
        return json.dumps({"status": "error", "message": "addresses 应为字符串列表"}, ensure_ascii=False)
    # 缓存：地址列表 hash 作 key（相同地址集直接返回，降配额、提速）
    cache_key = "geo:" + hashlib.md5("|".join(addresses).encode()).hexdigest()
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    res = []
    for addr in addresses:
        data = _request("/geocoding/v3", {"address": addr, "ret_coordtype": "bd09ll"})
        if data.get("status") == 0 and data.get("result"):
            loc = data["result"].get("location", {})
            res.append({"address": addr, "lng": loc.get("lng"), "lat": loc.get("lat"), "status": "ok"})
        else:
            res.append({"address": addr, "lng": None, "lat": None, "status": "fail",
                        "message": data.get("message", "")})
        time.sleep(0.2)  # 节流，避免超配额
    out = json.dumps({"count": len(res), "results": res}, ensure_ascii=False)
    _cache_set(cache_key, out)
    return out


def calc_distances(origin_lng: float, origin_lat: float, dests: list) -> str:
    """
    距离测算：计算仓库/门店(origin)到多个目标点(dests)的驾车距离，辅助供货调度。

    参数 origin_lng/origin_lat: 起点坐标
    参数 dests: 目标点列表 [{"lng":..., "lat":...} , ...] 或 [lng,lat] 对
    """
    if not dests:
        return json.dumps({"status": "error", "message": "dests 不能为空"}, ensure_ascii=False)
    dest_coords = []
    for d in dests:
        if isinstance(d, dict):
            dest_coords.append(f"{d['lat']},{d['lng']}")
        elif isinstance(d, (list, tuple)):
            dest_coords.append(f"{d[1]},{d[0]}")
    data = _request("/distancematrix/v1/driving", {
        "origins": f"{origin_lat},{origin_lng}",
        "destinations": "|".join(dest_coords),
    })
    if data.get("status") != 0:
        return json.dumps(data, ensure_ascii=False)
    rows = data.get("result", {}).get("rows", [])
    elems = rows[0].get("elements", []) if rows else []
    out = []
    for i, e in enumerate(elems):
        dist = e.get("distance", {}).get("value", 0)
        dur = e.get("duration", {}).get("value", 0)
        out.append({
            "dest_index": i, "dest": dest_coords[i] if i < len(dest_coords) else None,
            "distance_m": dist, "duration_s": dur,
        })
    total_route = sum(d["distance_m"] for d in out)
    return json.dumps({
        "origin": {"lng": origin_lng, "lat": origin_lat},
        "routes": out,
        "total_distance_m": None if not total_route else total_route,
    }, ensure_ascii=False)
