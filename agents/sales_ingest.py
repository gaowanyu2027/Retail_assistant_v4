"""
真实销量接入（POS）— 让归因分析摆脱「演示数据」

问题背景：
    转化率四象限、动线价值等归因能力都依赖销量，但项目内提供的是
    simulate_demo_sales() 生成的**演示数据**（period_key="demo"）。
    在演示数据上得出的"高热度低销量"等结论只是功能演示，
    不能作为经营决策依据——而此前系统并不区分、也不标注数据来源。

本模块提供：
    1. 真实销量导入（POS 导出 / 人工盘点 / CSV）：period_key 采用与视频管线
       一致的 YYYYMMDDHH 整点口径 → 既能与热度同口径比对，也能参与同期对比
    2. 数据来源判定（pos / simulated）：让归因结果明确标注数据来源，
       避免把演示结论当成真实经营结论

口径约定：period_key 以 "demo" 开头 = 演示数据（沿用既有 simulate_demo_sales /
seed_traffic_demo 的命名），其余 = 真实接入数据。
"""
from datetime import datetime

# 演示数据命名前缀（沿用既有约定，无需改表结构即可判定来源）
DEMO_PREFIX = "demo"

SOURCE_POS = "pos"              # 真实接入（POS / 人工录入）
SOURCE_SIMULATED = "simulated"  # 演示/模拟数据
SOURCE_TEST = "test"            # 验证期写入的测试数据（真实格式 period_key，但非真实业务数据）
SOURCE_MIXED = "mixed"          # 混合（同一窗口内既有真实又有演示/测试）
SOURCE_NONE = "none"            # 无数据
SOURCE_UNKNOWN = "unknown"      # **判定失败**（既不是"真实"，也不是"没有"）


def classify_period_key(period_key: str | None) -> str:
    """按 period_key 前缀**推测**来源（兼容旧库的兜底手段）。

    注意：这只是兜底——period_key 是真实整点格式时，**无法区分**「真实数据」与
    「用真实格式写入的测试数据」。正式判定请用显式的 `product_sales.source` 字段
    （见 classify_sources）。
    """
    if not period_key:
        return SOURCE_NONE
    return SOURCE_SIMULATED if str(period_key).startswith(DEMO_PREFIX) else SOURCE_POS


def classify_period_keys(period_keys) -> str:
    """按 period_key 前缀聚合判定来源（兼容旧库）。"""
    keys = [k for k in (period_keys or []) if k]
    if not keys:
        return SOURCE_NONE
    return classify_sources([classify_period_key(k) for k in keys])


def classify_sources(sources) -> str:
    """按**显式 source 字段**聚合判定来源（权威口径，优先使用）。"""
    vals = {s for s in (sources or []) if s}
    if not vals:
        return SOURCE_NONE
    if len(vals) > 1:
        return SOURCE_MIXED
    return vals.pop()


def source_caveat(source: str) -> str:
    """给归因结论附带的数据来源说明（**仅"已确认为真实数据"时为空串**）。"""
    if source == SOURCE_SIMULATED:
        return "销量为**演示数据**，结论仅用于功能验证，不可作为经营决策依据。"
    if source == SOURCE_TEST:
        return ("销量为**验证测试数据**（非真实业务数据），不可作为经营决策依据；"
                "如需真实结论请先接入 POS 数据。")
    if source == SOURCE_MIXED:
        return "该时段销量**混有非真实数据**（演示或测试），结论可能失真，建议核对数据来源。"
    if source == SOURCE_NONE:
        return "该时段没有销量记录，转化率/四象限结论不完整。"
    if source == SOURCE_UNKNOWN:
        # ⚠ 必须返回**非空**：调用方（sales_analytics）在来源判定失败时会落到 unknown，
        # 而空串的语义是"已确认为真实数据、无需提示"。若 unknown 也返回空串，
        # "无法确认来源"与"已确认真实"就完全无法区分——实测正是如此：
        #     sales_source = unknown，data_caveat = ''（与真实 POS 数据的输出一模一样）
        return ("⚠ **销量数据来源无法确认**（读取来源标记失败）——无法判断是真实 POS 数据"
                "还是演示/测试数据，请勿据此做经营决策；建议核对数据库连接与 "
                "product_sales.source 字段。")
    return ""


def period_key_now(dt: datetime | None = None) -> str:
    """当前整点时段标识（与视频管线写入口径一致）。"""
    return (dt or datetime.now()).strftime("%Y%m%d%H")


def parse_sales_csv(text: str) -> list[dict]:
    """解析销量 CSV。

    支持表头（大小写不敏感，可省略）：
        zone_id,sold_count,sales_amount

    Returns:
        [{"zone_id": str, "sold_count": int, "sales_amount": float}, ...]

    Raises:
        ValueError: 行格式/数值非法时（附带行号，便于定位）
    """
    rows: list[dict] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]

    for idx, line in enumerate(lines, start=1):
        parts = [p.strip() for p in line.split(",")]
        if idx == 1 and parts and parts[0].lower() in ("zone_id", "zone", "区域"):
            continue  # 表头
        if len(parts) < 3:
            raise ValueError(f"第 {idx} 行格式错误（需 zone_id,sold_count,sales_amount）：{line}")
        zone_id = parts[0]
        if not zone_id:
            raise ValueError(f"第 {idx} 行 zone_id 为空")
        try:
            sold = int(float(parts[1]))
            amount = float(parts[2])
        except ValueError:
            raise ValueError(f"第 {idx} 行数值非法：{line}")
        if sold < 0 or amount < 0:
            raise ValueError(f"第 {idx} 行出现负数：{line}")
        rows.append({"zone_id": zone_id, "sold_count": sold, "sales_amount": amount})

    if not rows:
        raise ValueError("没有可导入的有效数据行")
    return rows


def import_sales(records: list[dict], period_key: str | None = None,
                 source: str = SOURCE_POS) -> dict:
    """批量导入真实销量。

    Args:
        records: [{"zone_id", "sold_count", "sales_amount"}, ...]
        period_key: 时段标识；缺省取当前整点（YYYYMMDDHH）
        source: 来源标记，默认 pos（真实接入）；测试数据请显式传 SOURCE_TEST

    Returns:
        {"imported": n, "period_key": pk, "source": source, "zones": [...]}

    Raises:
        ValueError: 记录为空 / 字段非法 / 试图写入演示命名空间
    """
    import mysql_db

    if not records:
        raise ValueError("records 为空")

    pk = period_key or period_key_now()
    # 防呆：真实数据不得写进演示命名空间，否则会被判为演示数据污染归因
    if str(pk).startswith(DEMO_PREFIX):
        raise ValueError(
            f"period_key '{pk}' 属于演示数据命名空间（{DEMO_PREFIX}*），"
            "真实销量请使用整点时段标识（YYYYMMDDHH）"
        )

    normalized = []
    for i, r in enumerate(records, start=1):
        zone_id = str(r.get("zone_id") or "").strip()
        if not zone_id:
            raise ValueError(f"第 {i} 条记录缺少 zone_id")
        try:
            sold = int(r.get("sold_count") or 0)
            amount = float(r.get("sales_amount") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"第 {i} 条记录数值非法：{r}")
        if sold < 0 or amount < 0:
            raise ValueError(f"第 {i} 条记录出现负数：{r}")
        normalized.append({"zone_id": zone_id, "sold_count": sold, "sales_amount": amount})

    for r in normalized:
        mysql_db.save_product_sales(
            zone_id=r["zone_id"],
            period_key=pk,
            sold_count=r["sold_count"],
            sales_amount=r["sales_amount"],
            source=source,
        )

    return {
        "imported": len(normalized),
        "period_key": pk,
        "source": source,
        "zones": [r["zone_id"] for r in normalized],
    }
