"""
热度 vs 销量比对分析 — 现象归因（货架吸引力 × 商品转化）

业务洞察：货架热度高（停留人次多、停留时长长）≠ 销量高。
- 高热度 + 低销量：货架吸客能力强，但商品品质/匹配度/陈列未达顾客选择标准
  （"挑了很久却没买"）→ 建议检查商品品质、缺货、价格
- 低热度 + 高销量：商品本身有竞争力，但货架曝光/吸引力不足
  （顾客目标明确直奔购买）→ 建议增强陈列、导购、促销
- 高热度 + 高销量：健康区
- 低热度 + 低销量：冷区，需整体优化

数据源：
- 热度侧：retail_stats（视频管线实时统计）
- 销量侧：product_sales（POS 接入或演示数据录入）
"""
import json
from statistics import median


def compare_hotness_vs_sales(period_key: str | None = None, hours: int = 1) -> dict:
    """对齐同一时段的热度与销量，计算转化率并做四象限诊断。

    返回：
    {
        "period": "...", "zones": [{zone_id, zone_label, visit_count,
            avg_dwell_seconds, heat_score, sold_count, sales_amount,
            conversion_rate, quadrant, diagnosis, suggestion}, ...],
        "summary": "整体结论",
    }
    """
    import mysql_db

    hot = {z["zone_id"]: z for z in mysql_db.get_retail_stats_by_zone(period_key, hours)}
    sales = {s["zone_id"]: s for s in mysql_db.get_product_sales(period_key, hours)}

    # 数据来源判定：四象限依赖销量，必须让使用者知道结论是建立在真实数据还是演示/测试数据上。
    # 优先用**显式 source 字段**（权威口径）；旧库缺该字段时退回 period_key 前缀推测。
    try:
        from agents.sales_ingest import (SOURCE_UNKNOWN, classify_period_keys,
                                         classify_sources, source_caveat)
        try:
            sources = mysql_db.get_sales_sources(period_key, hours)
        except Exception:
            sources = []
        if sources:
            sales_source = classify_sources(sources)
        else:
            sales_source = classify_period_keys(
                mysql_db.get_sales_period_keys(period_key, hours))
        caveat = source_caveat(sales_source)
    except Exception as e:
        # ⚠ 判定失败时**必须给非空提示**：原先这里是 `sales_source, caveat = "unknown", ""`，
        # 而空串的语义是"已确认为真实数据、无需提示"——于是"来源无法确认"被伪装成
        # "已确认真实"，与真实 POS 数据的输出**一模一样**（实测确认）。
        sales_source = SOURCE_UNKNOWN
        caveat = (f"⚠ **销量数据来源无法确认**（{type(e).__name__}）——无法判断是真实业务数据"
                  "还是演示/测试数据，请勿据此做经营决策；建议核对数据库连接与 "
                  "product_sales.source 字段。")

    all_zones = sorted(set(hot) | set(sales))
    zones_out = []
    for zid in all_zones:
        h = hot.get(zid, {})
        s = sales.get(zid, {})
        visit = int(h.get("visit_count", 0))
        sold = int(s.get("sold_count", 0))
        dwell = float(h.get("total_dwell_seconds", 0))
        heat = float(h.get("heat_score", 0))
        avg_dwell = round(dwell / visit, 1) if visit else 0.0
        conversion = round(sold / visit * 100, 1) if visit else 0.0

        quadrant, diagnosis, suggestion = _diagnose(visit, sold, heat, avg_dwell)
        zones_out.append({
            "zone_id": zid,
            "zone_label": h.get("zone_label", zid),
            "visit_count": visit,
            "avg_dwell_seconds": avg_dwell,
            "heat_score": heat,
            "sold_count": sold,
            "sales_amount": float(s.get("sales_amount", 0)),
            "conversion_rate": conversion,
            # >100% 时的口径说明（否则空串）：字段名沿用 conversion_rate 以兼容前端，
            # 但这里讲清它其实是"件/人比"——件数/人次，同一顾客买多件就会 >100%。
            "conversion_note": (
                f"销量按**件**计、客流按**人次**计，{sold} 件 / {visit} 人次 = {conversion:.0f}% "
                f"属「件/人比」而非转化率（同一顾客可购买多件）；"
                f"真实转化率需按成交**笔数**统计，当前数据未提供。"
                if sold > visit else ""
            ),
            "quadrant": quadrant,
            "diagnosis": diagnosis,
            "suggestion": suggestion,
        })

    summary = _summarize(zones_out)
    return {
        "period": period_key or f"最近 {hours} 小时",
        "zones": zones_out,
        "summary": summary,
        "sales_source": sales_source,     # pos=真实接入 / simulated=演示 / mixed / none
        "data_caveat": caveat,            # 真实数据时为空串；演示数据时明确提示
    }


def _diagnose(visit: int, sold: int, heat: float, avg_dwell: float):
    """单区域四象限判定（热度/销量按 0 与均值分界，阈值可调）。"""
    has_hot = visit > 0
    has_sales = sold > 0

    if not has_hot and not has_sales:
        return ("cold", "该区域近期无客流也无销量记录", "建议整体优化陈列与商品配置")
    if not has_hot:
        return ("low_heat_high_sales",
                "无客流统计但存在销量（数据可能来自非监控时段或补录）",
                "建议核对客流统计覆盖，确认是否漏采")
    if not has_sales:
        return ("high_heat_no_sales",
                f"客流 {visit} 人次但无销量记录，货架吸引力强而转化完全缺失",
                "建议优先检查商品品质/缺货/价格，并确认销量数据是否已录入")

    conversion = sold / visit
    # ⚠ 口径（重要，别再退回"一律叫转化率"）：
    # sold_count 是**件数**、visit 是**人次**，所以 sold/visit 严格来说是「**件/人比**」，
    # 只有当 sold <= visit 时才能近似当作"转化率"。
    # 原先一律叫"转化率"并直接 *100，实测 10 人次 / 45 件 -> 450%，
    # 还被判定为"吸引与转化俱佳"——**一个自相矛盾的结论**（转化率不可能 >100%）。
    # 这里按实际大小动态取标签；数值本身仍返回（前端在用 conversion_rate 字段），
    # 另在 zones 里补 conversion_note 说明口径。
    metric = "件/人比" if sold > visit else "转化率"
    # 高热度判定：平均停留超过 30 秒视为"深度停留"（吸引力信号）
    high_heat = visit >= 10 and avg_dwell >= 30
    high_sales = conversion >= 0.3  # >=30% 视为高转化（件/人比口径下同样适用）
    pct = conversion * 100

    if high_heat and high_sales:
        return ("healthy",
                f"客流 {visit} 人次、{metric} {pct:.0f}%，吸引与转化俱佳",
                "保持当前陈列与商品策略，可考虑追加补货")
    if high_heat and not high_sales:
        return ("high_heat_low_sales",
                f"客流 {visit} 人次（平均停留 {avg_dwell:.0f} 秒）但{metric}仅 {pct:.0f}%："
                "货架吸引力强，顾客停留挑选却未购买——商品品质/匹配度未达选择标准",
                "重点排查该货架商品品质、缺货、价格竞争力与陈列次序")
    if not high_heat and high_sales:
        return ("low_heat_high_sales",
                f"{metric} {pct:.0f}% 高但客流偏低：商品有竞争力，货架曝光/吸引力不足",
                "增强陈列吸引力、增加促销标识或引导客流到该区域")
    return ("low_heat_low_sales",
            f"客流 {visit} 人次、{metric} {pct:.0f}%，双低",
            "从商品配置与陈列双方面整体优化，并结合时段客流做针对性调整")


def _summarize(zones: list[dict]) -> str:
    if not zones:
        return "暂无区域热度/销量数据，无法比对。请先运行视频分析并录入销量。"
    with_sales = [z for z in zones if z["sold_count"] > 0]
    if not with_sales:
        return "已获取区域热度，但未录入任何销量数据——无法完成热度 vs 销量比对。"
    problems = [z for z in zones if z["quadrant"] in ("high_heat_low_sales", "low_heat_high_sales")]
    if not problems:
        return "各区域热度与销量匹配良好，未发现明显转化异常。"
    parts = []
    for z in problems:
        parts.append(f"{z['zone_label']}({z['quadrant']})")
    return "发现转化异常区域：" + "、".join(parts) + "。详见各区域诊断。"


def simulate_demo_sales():
    """生成一组演示数据，直观体现"热度 vs 销量"四象限业务场景。

    同时写入视频热度（retail_stats）与销量（product_sales），同一 period_key：
    - 1号货架（零食区）：高热度（客流 120、平均停留 50s）+ 低销量 8 → 吸引强转化弱（商品品质疑点）
    - 2号货架（饮料区）：低热度（客流 60、平均停留 17s）+ 高销量 45 → 转化强曝光不足
    - 3号货架（日用品区）：高热度 + 均衡销量 → 健康

    注意：演示热度会覆盖同分钟的真实视频统计（仅演示场景使用）。
    """
    import mysql_db
    from datetime import datetime

    now = datetime.now()
    period_key = "demo"  # 固定演示 key：每次生成先清旧数据，防止跨分钟累积
    start = now.strftime("%Y-%m-%d %H:%M:00")
    end = now.strftime("%Y-%m-%d %H:%M:59")

    # 清理旧的演示数据（热度 + 销量），保证结果可复现
    conn = mysql_db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM product_sales WHERE period_key=%s", (period_key,))
            cur.execute("DELETE FROM retail_stats WHERE period_key=%s", (period_key,))
    finally:
        conn.close()

    demo_zones = {
        "shelf_A": {"zone_label": "1号货架 - 零食区", "visit_count": 120,
                    "total_dwell_seconds": 6000, "heat_score": 88},
        "shelf_B": {"zone_label": "2号货架 - 饮料区", "visit_count": 60,
                    "total_dwell_seconds": 1000, "heat_score": 40},
        "shelf_C": {"zone_label": "3号货架 - 日用品区", "visit_count": 90,
                    "total_dwell_seconds": 3600, "heat_score": 78},
    }
    demo_sales = [
        ("shelf_A", 8, 68.0),    # 高热度低销量
        ("shelf_B", 45, 520.0),  # 低热度高销量
        ("shelf_C", 35, 310.0),  # 健康
    ]
    # 显式标记为演示数据（不依赖 period_key 前缀被猜出来）——热度侧以前没有来源字段，
    # 演示数据会被时段分析当成真实客流，台账 A8 已补齐
    mysql_db.save_retail_stats(period_key, demo_zones, start, end, source="simulated")
    for zone_id, sold, amount in demo_sales:
        mysql_db.save_product_sales(zone_id, period_key, sold, amount, start, end,
                                    source="simulated")
    return len(demo_sales)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(compare_hotness_vs_sales(), ensure_ascii=False, indent=2))
