"""
同期对比 — 回答「比昨天/上周同期怎么样」

业务动机：
    单点数字没有决策价值。店长每天真正问的是「今天比昨天好还是差」。
    项目此前把「昨天/上周」这类问题直接丢给 LLM 并回答「系统只有实时数据」，
    但 retail_stats / product_sales 实际按 period_key(YYYYMMDDHH) 留存了 30 天——
    **数据在库里，只是缺查询入口**。

实现：
    复用 mysql_db 现成的「按 period_key 聚合」接口，对「当前时段」与
    「昨天同期 / 上周同期」做同口径对比，输出差值、变化率与结论。
    对比口径一致（同为整点小时段），避免拿今天 1 小时比昨天全天这类错误。
"""
from datetime import datetime, timedelta

# 对比口径：小时偏移 → 标签（period_key 为整点，故偏移均为 24 的倍数）
COMPARE_OFFSETS: tuple[tuple[int, str], ...] = (
    (24, "昨天同期"),
    (168, "上周同期"),
)

# 变化率超过该百分比才判定为「上升/下降」，否则视为持平（抑制噪声抖动）
FLAT_THRESHOLD_PCT = 5.0


def _period_key(dt: datetime) -> str:
    """period_key 口径：与视频管线写入保持一致（YYYYMMDDHH）。"""
    return dt.strftime("%Y%m%d%H")


def _aggregate(period_key: str) -> dict:
    """聚合某个时段的视频热度与销量（同口径指标）。"""
    import mysql_db

    try:
        stats = mysql_db.get_retail_stats_by_zone(period_key=period_key)
    except Exception:
        stats = []
    try:
        sales = mysql_db.get_product_sales(period_key=period_key)
    except Exception:
        sales = []

    return {
        "period_key": period_key,
        "has_data": bool(stats) or bool(sales),
        "zone_count": len(stats),
        "visit_count": sum(int(z.get("visit_count", 0) or 0) for z in stats),
        "dwell_seconds": round(
            sum(float(z.get("total_dwell_seconds", 0) or 0) for z in stats), 1),
        "sold_count": sum(int(s.get("sold_count", 0) or 0) for s in sales),
        "sales_amount": round(
            sum(float(s.get("sales_amount", 0) or 0) for s in sales), 2),
    }


def _pct(current: float, base: float) -> float | None:
    """变化率（%）；基数为 0 时无意义，返回 None。"""
    if not base:
        return None
    return round((current - base) / base * 100, 1)


def _delta(current: float, base: float) -> float:
    return round(current - base, 2)


def _trend_text(pct: float | None) -> str:
    if pct is None:
        return "无对比基线（基数为 0）"
    if pct > FLAT_THRESHOLD_PCT:
        return f"上升 {pct}%"
    if pct < -FLAT_THRESHOLD_PCT:
        return f"下降 {abs(pct)}%"
    return f"基本持平（{pct}%）"


def _conclude(current: dict, comparisons: list[dict]) -> str:
    if not current.get("has_data"):
        return ("当前时段没有采集到数据，无法给出同期对比结论"
                "（请先确认摄像头/视频源是否正常）。")

    parts = []
    for c in comparisons:
        if not c["has_data"]:
            parts.append(f"{c['label']}无记录，缺少对比基线")
            continue
        parts.append(
            f"较{c['label']}到访{_trend_text(c['visit_pct'])}"
            f"（{c['visit_count']} → {current['visit_count']} 人次）"
        )
    return "；".join(parts) + "。"


def compare_periods(reference: datetime | None = None,
                    offsets: tuple[tuple[int, str], ...] = COMPARE_OFFSETS) -> dict:
    """对比「当前时段」与「昨天同期 / 上周同期」。

    Args:
        reference: 参照时刻（默认当前时间；测试可注入固定时刻）
        offsets: (小时偏移, 标签) 列表

    Returns:
        {
          "current": {该时段聚合指标},
          "comparisons": [{label, 基线值, 差值, 变化率}],
          "conclusion": "一句话结论",
          "note": "特殊情况说明"
        }
    """
    now = reference or datetime.now()
    current = _aggregate(_period_key(now))

    comparisons = []
    for hours, label in offsets:
        base = _aggregate(_period_key(now - timedelta(hours=hours)))
        comparisons.append({
            "label": label,
            "period_key": base["period_key"],
            "has_data": base["has_data"],
            "visit_count": base["visit_count"],
            "visit_delta": _delta(current["visit_count"], base["visit_count"]),
            "visit_pct": _pct(current["visit_count"], base["visit_count"]),
            "dwell_seconds": base["dwell_seconds"],
            "dwell_delta": _delta(current["dwell_seconds"], base["dwell_seconds"]),
            "dwell_pct": _pct(current["dwell_seconds"], base["dwell_seconds"]),
            "sold_count": base["sold_count"],
            "sold_pct": _pct(current["sold_count"], base["sold_count"]),
            "sales_amount": base["sales_amount"],
            "sales_pct": _pct(current["sales_amount"], base["sales_amount"]),
        })

    return {
        "current": current,
        "comparisons": comparisons,
        "conclusion": _conclude(current, comparisons),
        "note": ("" if current.get("has_data")
                 else "当前时段无数据：对比结果仅反映历史时段值"),
    }
