"""
时段客流分析 + 深度兴趣 vs 快速路过

- hourly_traffic: 按小时聚合客流（visit/deep/dwell），输出 24h 曲线 + 高峰/低谷结论
                  （排班/补货/促销时段决策）
- hourly_alerts:  按小时聚合告警数（安保重点时段）
- zone_depth:     区域"选购浓度"（深度兴趣占比）× 销量 四象限
                  （区分"看了不买=商品问题" vs "没看就买=刚需品" vs "纯路过"）
"""
from datetime import datetime, timedelta


def hourly_traffic(hours: int = 24) -> dict:
    """按小时聚合客流。返回 {"hours": [{hour, visit_count, deep_interest_count,
    total_dwell_seconds, heat_score}, ...], "peak": "...", "valley": "...", "summary": "..."}"""
    import mysql_db
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = mysql_db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DATE_FORMAT(period_start, '%%Y-%%m-%%d %%H:00') AS hr,"
                " SUM(visit_count), SUM(deep_interest_count),"
                " SUM(total_dwell_seconds), MAX(heat_score)"
                " FROM retail_stats WHERE period_end >= %s GROUP BY hr ORDER BY hr",
                (cutoff,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    hours_out = [{
        "hour": r[0], "visit_count": int(r[1] or 0),
        "deep_interest_count": int(r[2] or 0),
        "total_dwell_seconds": round(float(r[3] or 0), 1),
        "heat_score": round(float(r[4] or 0), 1),
    } for r in rows]

    if not hours_out:
        return {"hours": [], "peak": "暂无数据", "valley": "暂无数据",
                "summary": "暂无客流时段数据（需运行视频采集）"}

    peak = max(hours_out, key=lambda h: h["visit_count"])
    valley = min(hours_out, key=lambda h: h["visit_count"])
    total = sum(h["visit_count"] for h in hours_out)
    summary = (
        f"近 {hours} 小时累计到访 {total} 人次。"
        f"客流高峰在 {peak['hour'][11:16]}（{peak['visit_count']} 人次），"
        f"低谷在 {valley['hour'][11:16]}（{valley['visit_count']} 人次）。"
        "建议高峰时段增派补货/收银，低谷时段安排陈列调整。"
    )
    return {"hours": hours_out,
            "peak": f"{peak['hour'][11:16]} ({peak['visit_count']} 人次)",
            "valley": f"{valley['hour'][11:16]} ({valley['visit_count']} 人次)",
            "summary": summary}


def hourly_alerts(hours: int = 24) -> dict:
    """按小时聚合告警数（安保重点时段）。"""
    import mysql_db
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = mysql_db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:00') AS hr, COUNT(*)"
                " FROM alert_record WHERE created_at >= %s GROUP BY hr ORDER BY hr",
                (cutoff,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    hours_out = [{"hour": r[0], "alert_count": int(r[1])} for r in rows]
    total = sum(h["alert_count"] for h in hours_out)
    peak = max(hours_out, key=lambda h: h["alert_count"]) if hours_out else None
    return {
        "hours": hours_out,
        "total_alerts": total,
        "peak_hour": f"{peak['hour'][11:16]} ({peak['alert_count']} 起)" if peak else "暂无数据",
        "summary": (
            f"近 {hours} 小时共 {total} 起告警"
            + (f"，集中在 {peak['hour'][11:16]}（{peak['alert_count']} 起），建议该时段加强巡查" if peak else "")
        ),
    }


def zone_depth(hours: int = 1) -> dict:
    """区域"选购浓度"（深度兴趣占比）× 销量 四象限。

    快速路过 = visit - deep（停留<30s）。
    判定：
    - deep 占比高 + 销量高 → 健康
    - deep 占比高 + 销量低 → 看了不买（商品品质/价格问题）
    - deep 占比低 + 销量高 → 刚需高频品（拿完就走，补货+位置）
    - deep 占比低 + 销量低 → 纯路过（陈列/引流问题或通道）
    """
    import mysql_db

    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = mysql_db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT zone_id, MAX(zone_label), SUM(visit_count),"
                " SUM(deep_interest_count), SUM(total_dwell_seconds)"
                " FROM retail_stats WHERE period_end >= %s GROUP BY zone_id",
                (cutoff,),
            )
            hot_rows = cur.fetchall()
            cur.execute(
                "SELECT zone_id, SUM(sold_count) FROM product_sales"
                " WHERE period_end >= %s GROUP BY zone_id",
                (cutoff,),
            )
            sales_rows = cur.fetchall()
    finally:
        conn.close()

    sales = {r[0]: int(r[1] or 0) for r in sales_rows}
    zones_out = []
    for zid, label, visit, deep, dwell in hot_rows:
        visit = int(visit or 0)
        deep = int(deep or 0)
        pass_by = max(visit - deep, 0)
        depth_rate = round(deep / visit * 100, 1) if visit else 0.0
        sold = sales.get(zid, 0)
        conversion = round(sold / visit * 100, 1) if visit else 0.0
        quadrant, diag = _depth_quadrant(visit, deep, depth_rate, sold, conversion)
        zones_out.append({
            "zone_id": zid, "zone_label": label or zid,
            "visit_count": visit, "deep_interest_count": deep,
            "pass_by_count": pass_by, "depth_rate": depth_rate,
            "sold_count": sold, "conversion_rate": conversion,
            "quadrant": quadrant, "diagnosis": diag,
        })

    return {"zones": zones_out, "summary": _depth_summary(zones_out)}


def _depth_quadrant(visit: int, deep: int, depth_rate: float, sold: int, conversion: float):
    has_deep = deep > 0
    has_sales = sold > 0
    if not has_deep and not has_sales:
        return ("cold", "无深度停留也无销量（纯路过/空置）")
    if not has_deep:
        if has_sales:
            return ("low_depth_high_sales",
                    f"快速路过为主（{visit} 人次停留均<30s）但销量 {sold} 件：刚需高频品，顾客目标明确拿了就走",
                    )
        return ("low_depth_low_sales",
                f"快速路过 {visit} 人次且无销量：纯路过区域，无停留价值也无转化")
    # 有深度兴趣
    if depth_rate >= 30.0:  # 深度占比 ≥30% 视为"认真选购区"
        if has_sales and conversion >= 30.0:
            return ("healthy", f"深度选购占比 {depth_rate:.0f}% 且转化率 {conversion:.0f}%：认真选购+转化俱佳")
        return ("high_depth_low_sales",
                f"深度选购占比 {depth_rate:.0f}% 但销量仅 {sold} 件：顾客停留挑选却未购买——商品品质/价格/匹配度问题")
    # 深度占比低但有销量
    if has_sales:
        return ("low_depth_high_sales",
                f"深度占比仅 {depth_rate:.0f}% 但销量 {sold} 件：刚需高频品（目标明确快速购买），"
                "重点保障补货与位置显眼，无需过度陈列")
    return ("low_depth_low_sales", f"深度占比 {depth_rate:.0f}% 且无销量：区域吸引力与转化均不足")


def _depth_summary(zones: list[dict]) -> str:
    if not zones:
        return "暂无区域深度/销量数据（需运行视频采集并录入销量）"
    problems = [z for z in zones if z["quadrant"] in ("high_depth_low_sales", "low_depth_low_sales")]
    staples = [z for z in zones if z["quadrant"] == "low_depth_high_sales"]
    parts = []
    if problems:
        parts.append("待优化区域：" + "、".join(f"{z['zone_label']}({z['quadrant']})" for z in problems))
    if staples:
        parts.append("刚需高频区域（快速购买）：" + "、".join(z["zone_label"] for z in staples))
    return "；".join(parts) if parts else "各区域深度与销量匹配良好"


def seed_traffic_demo() -> int:
    """生成测试用时段客流+深度模拟数据（测试用途，非生产）。

    构造可验证的模式：
    - 24 小时客流曲线：高峰 18-20 点、低谷 3-5 点（排班决策可验证）
    - 区域深度特征：
      shelf_A 高深度低销量（看了不买=商品问题）
      shelf_B 低深度高销量（刚需高频，拿完就走）
      shelf_C 健康（深度+转化俱佳）
    """
    import mysql_db
    from datetime import datetime, timedelta

    now = datetime.now()
    # 清理旧 demo 数据（traffic 模拟写入零售/销量/告警）
    conn = mysql_db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM retail_stats WHERE period_key LIKE 'demo_traffic%'")
            cur.execute("DELETE FROM product_sales WHERE period_key LIKE 'demo_traffic%'")
            cur.execute("DELETE FROM alert_record WHERE zone_id='demo_traffic'")
    finally:
        conn.close()

    # 24 小时客流基线：高峰 18-20 点，低谷 3-5 点
    def hour_factor(h):
        if 18 <= h <= 20:
            return 1.0
        if 11 <= h <= 14:
            return 0.7
        if 3 <= h <= 5:
            return 0.08
        return 0.35

    count = 0
    for i in range(24):
        t = now - timedelta(hours=23 - i)
        f = hour_factor(t.hour)
        period_key = f"demo_traffic_{t.strftime('%Y%m%d%H')}"
        start = t.strftime("%Y-%m-%d %H:%M:00")
        end = t.strftime("%Y-%m-%d %H:%M:59")
        zones = {
            "shelf_A": {"zone_label": "1号货架 - 零食区", "visit_count": int(50 * f),
                        "deep_interest_count": int(38 * f),   # 深度占比 ~76%：认真选购
                        "total_dwell_seconds": int(4200 * f), "heat_score": 80},
            "shelf_B": {"zone_label": "2号货架 - 饮料区", "visit_count": int(45 * f),
                        "deep_interest_count": int(8 * f),    # 深度占比 ~18%：快速路过为主
                        "total_dwell_seconds": int(900 * f), "heat_score": 55},
            "shelf_C": {"zone_label": "3号货架 - 日用品区", "visit_count": int(35 * f),
                        "deep_interest_count": int(18 * f),   # 深度占比 ~51%
                        "total_dwell_seconds": int(2600 * f), "heat_score": 68},
        }
        mysql_db.save_retail_stats(period_key, zones, start, end)
        count += 3

    # 最近时段销量（与深度特征对照）—— 显式标记为演示数据
    now_key = f"demo_traffic_{now.strftime('%Y%m%d%H')}"
    _s, _e = now.strftime("%Y-%m-%d %H:%M:00"), now.strftime("%Y-%m-%d %H:%M:59")
    mysql_db.save_product_sales("shelf_A", now_key, 9, 88.0, _s, _e, source="simulated")
    mysql_db.save_product_sales("shelf_B", now_key, 40, 520.0, _s, _e, source="simulated")
    mysql_db.save_product_sales("shelf_C", now_key, 22, 260.0, _s, _e, source="simulated")
    count += 3

    # 告警时段分布：集中在晚间
    for i in range(24):
        t = now - timedelta(hours=23 - i)
        if 18 <= t.hour <= 22:
            for _ in range(2 if t.hour in (19, 20) else 1):
                mysql_db.save_alert_record(
                    alert_type="anomaly", zone_id="demo_traffic", person_id=i,
                    level="watch", score=55, reason="模拟测试告警", frame_id=i,
                    created_at=t.strftime("%Y-%m-%d %H:%M:%S"),
                )
                count += 1
    return count


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"traffic": hourly_traffic(24), "depth": zone_depth(24)}, ensure_ascii=False, indent=2))
