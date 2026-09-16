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
    """period_key 口径：**小时粒度**（YYYYMMDDHH），与销量侧保持一致。

    ⚠ 注意视频管线写入 `retail_stats` 时用的是 **12 位分钟**（YYYYMMDDHHMM），
    与本函数的 10 位小时 key **不同粒度**。这里不改写路径（它需要每分钟一行做
    UPSERT），而是由 `mysql_db.get_retail_stats_by_zone` 对短 key 做前缀匹配来兼容。
    历史上这里注释自称"与视频管线写入保持一致"，是错的——正因如此
    "客流同期对比"长期恒为 0（实测同刻销量 5 件、客流 0）。
    """
    return dt.strftime("%Y%m%d%H")


def _aggregate(period_key: str, until_key: str | None = None) -> dict:
    """聚合某个时段的视频热度与销量（同口径指标）。

    `until_key`：把热度窗口截断到"同一分钟"（见 compare_periods 的说明）。
    注意**销量无法按分钟截断**——`product_sales.period_key` 是 10 位小时粒度，
    一小时只有一行，所以销量对比在本小时未结束时仍是"整小时 vs 已过部分"，
    这一点会在 note 里明确说明，不让它冒充成等长对比。

    ⚠ **取数失败（如数据库不可用）必须与"确实没有数据"区分开**：
    原先两个 `try/except` 都把异常吞成空列表，于是 `has_data=False`，
    结论变成"当前时段没有采集到数据…请先确认摄像头/视频源是否正常"——
    **把数据库故障误诊成摄像头故障**，让人去查一个没问题的地方。
    实测（打桩让查询抛 `Can't connect to MySQL server`）输出的文案与真的没数据**完全一致**。

    故这里记录错误并以 `data_error` / `available` 暴露：
    - `available=False` → 数据**取不到**（不可用）
    - `available=True, has_data=False` → 数据取到了，但该时段**确实为空**
    这两者的处置完全不同，不能共用一个空列表表示。
    """
    import mysql_db

    errors: list[str] = []
    try:
        stats = mysql_db.get_retail_stats_by_zone(period_key=period_key,
                                                 until_key=until_key)
    except Exception as e:
        stats = []
        errors.append(f"retail_stats({type(e).__name__})")
    try:
        sales = mysql_db.get_product_sales(period_key=period_key)
    except Exception as e:
        sales = []
        errors.append(f"product_sales({type(e).__name__})")

    return {
        "period_key": period_key,
        "has_data": bool(stats) or bool(sales),
        # available=False 表示"取数失败"，不是"没有数据"
        "available": not errors,
        "data_error": "；".join(errors),
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
    # 先区分"取不到数据"与"确实没有数据"——两者对使用者的含义完全不同
    if not current.get("available", True):
        return (f"取数失败（{current.get('data_error') or '数据库不可用'}），"
                "无法给出同期对比结论。"
                "⚠ 这与「没有数据」不是一回事：请检查数据库连接后重试，"
                "不要据此判断摄像头/视频源有问题。")
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

    # ⚠「本小时只过了 N 分钟」必须与历史**相同已过分钟数**对齐（A12 修复）。
    # 否则会拿"本小时前 5 分钟"比"昨天整个小时"，实测 15:05 时显示 -93.3% 的假暴跌
    # —— 那个数字不是业务变化，纯粹是窗口长度不等造成的。
    # period_key 是 YYYYMMDDHHMM（可直接字典序比较），所以用一个上界把窗口截齐即可。
    minute = now.minute
    cur_key = _period_key(now)
    current = _aggregate(cur_key, until_key=f"{cur_key}{minute:02d}")

    comparisons = []
    for hours, label in offsets:
        base_key = _period_key(now - timedelta(hours=hours))
        # 历史基线同样截断到"同一分钟"，保证两个窗口等长
        base = _aggregate(base_key, until_key=f"{base_key}{minute:02d}")
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

    # note 也要区分"取数失败"与"没有数据"（见 _aggregate 的说明）
    if not current.get("available", True):
        note = (f"数据不可用：{current.get('data_error') or '数据库取数失败'}。"
                "对比结果无效，请检查数据库连接——不是「没有数据」。")
    elif not current.get("has_data"):
        note = "当前时段无数据：对比结果仅反映历史时段值"
    else:
        note = ""

    # 本小时未结束时补充窗口说明（A12）：热度已按同分钟数对齐，
    # 但销量是小时粒度、无法截断，不能让它冒充等长对比。
    if minute < 55:
        partial = (f"当前小时仅进行到第 {minute} 分钟：热度已与历史**相同已过分钟数**对齐"
                   f"（不是拿 {minute} 分钟比整小时）；"
                   "但销量是小时粒度、无法按分钟截断，销量变化率仍会系统性偏低，"
                   "建议整点后再看。样本偏小，结论仅供参考。")
        note = f"{note} {partial}".strip() if note else partial

    return {
        "current": current,
        "comparisons": comparisons,
        "conclusion": _conclude(current, comparisons),
        "note": note,
    }
