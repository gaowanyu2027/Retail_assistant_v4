"""
结构化运营档案 — 深度复盘记忆（替代"文本摘要丢数字"）

长会话 30 分钟深度复盘时，若靠文本摘要压缩会丢精确数字（"昨晚 20 点客流多少"）。
本模块把核心运营数据（客流/热度/销量/告警）按时间段聚合成"结构化档案"，
Agent 复盘时调用 get_ops_archive 精确查询，而非凭摘要猜。

数据源：retail_stats（客流/热度）、product_sales（销量）、heat_report（汇报）。
"""
import json


def _aggregate(hours: int) -> dict:
    """聚合近 N 小时运营档案（结构化）。"""
    import mysql_db

    hot = {z["zone_id"]: z for z in mysql_db.get_retail_stats_by_zone(None, hours)}
    sales = {s["zone_id"]: s for s in mysql_db.get_product_sales(None, hours)}

    zones = []
    for zid in sorted(set(hot) | set(sales)):
        h = hot.get(zid, {})
        s = sales.get(zid, {})
        zones.append({
            "zone_id": zid,
            "zone_label": h.get("zone_label", zid),
            "visit_count": int(h.get("visit_count", 0)),
            "total_dwell_seconds": round(float(h.get("total_dwell_seconds", 0)), 1),
            "heat_score": round(float(h.get("heat_score", 0)), 1),
            "sold_count": int(s.get("sold_count", 0)),
            "sales_amount": round(float(s.get("sales_amount", 0)), 2),
        })

    total_visits = sum(z["visit_count"] for z in zones)
    total_sold = sum(z["sold_count"] for z in zones)
    top = max(zones, key=lambda z: z["visit_count"]) if zones else None

    # 近 N 小时热度汇报摘要（定期汇报的 LLM 摘要）
    report_summary = ""
    try:
        reports = mysql_db.get_latest_heat_reports(limit=1)
        if reports:
            report_summary = reports[0].get("summary", "")
    except Exception:
        pass

    return {
        "period_hours": hours,
        "total_visits": total_visits,
        "total_sold": total_sold,
        "top_zone": top,
        "zones": zones,
        "report_summary": report_summary,
    }


def get_ops_archive(hours: int = 24, start_hour: str | None = None, end_hour: str | None = None) -> str:
    """生成近 N 小时结构化运营档案（供 Agent 深度复盘精确查询）。

    参数 hours: 回溯小时数（默认 24）
    参数 start_hour/end_hour: 可选，指定时段（如 "14:00"~"18:00"）；缺省用 hours
    """
    try:
        data = _aggregate(hours)
        if start_hour and end_hour:
            data["period_window"] = f"{start_hour}~{end_hour}"
        # 简单时段补充结论
        if data["zones"]:
            low = [z for z in data["zones"] if z["visit_count"] == 0]
            data["conclusion"] = (
                f"近 {hours} 小时累计到访 {data['total_visits']} 人次、销量 {data['total_sold']} 件。"
                f"客流集中在 {data['top_zone']['zone_label'] if data['top_zone'] else '无'}。"
                + (f"有 {len(low)} 个区域零到访，需排查陈列/客流引导。" if low else "")
            )
        else:
            data["conclusion"] = f"近 {hours} 小时无运营数据（需运行视频采集/录入销量）。"
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)
