"""热度 vs 销量四象限：口径与数据来源提示。

两个已修的坑：

1. **件/人比不是转化率**：`sold_count` 是件数、`visit_count` 是人次，
   10 人次 / 45 件 曾输出"转化率 450%、吸引与转化俱佳"——一个自相矛盾的结论。
   现在 >100% 时动态改称「件/人比」，并在 `conversion_note` 里说明。
2. **来源判定失败必须给非空提示**：空串的语义是"已确认真实数据"；
   判定失败若也返回空串，"无法确认来源"会和真实 POS 数据长得一模一样。

用假 `mysql_db` 模块，离线运行。
"""
import contextlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents import sales_analytics as sa  # noqa: E402

HOT = [
    {"zone_id": "shelf_A", "zone_label": "1号货架", "visit_count": 120,
     "total_dwell_seconds": 6000, "heat_score": 88},
    {"zone_id": "shelf_B", "zone_label": "2号货架", "visit_count": 60,
     "total_dwell_seconds": 1000, "heat_score": 40},
    {"zone_id": "shelf_C", "zone_label": "3号货架", "visit_count": 90,
     "total_dwell_seconds": 3600, "heat_score": 78},
]
SALES = [
    {"zone_id": "shelf_A", "sold_count": 8, "sales_amount": 68.0},
    {"zone_id": "shelf_B", "sold_count": 45, "sales_amount": 520.0},
    {"zone_id": "shelf_C", "sold_count": 35, "sales_amount": 310.0},
]


class _FakeMySQL:
    def __init__(self, hot=None, sales=None, sources=("pos",), period_keys=None):
        self.hot = list(hot or [])
        self.sales = list(sales or [])
        self.sources = list(sources)
        self.period_keys = list(period_keys or [])
        self.sources_error = False
        self.period_keys_error = False

    def get_retail_stats_by_zone(self, period_key=None, hours=1):
        return list(self.hot)

    def get_product_sales(self, period_key=None, hours=1):
        return list(self.sales)

    def get_sales_sources(self, period_key=None, hours=1):
        if self.sources_error:
            raise RuntimeError("no such column: source")
        return list(self.sources)

    def get_sales_period_keys(self, period_key=None, hours=1):
        if self.period_keys_error:
            raise RuntimeError("query failed")
        return list(self.period_keys)


class _LegacyMySQL:
    """旧库：两个来源判定接口都不存在 → 走"无法确认"路径。"""

    def __init__(self, hot, sales):
        self.hot = hot
        self.sales = sales

    def get_retail_stats_by_zone(self, period_key=None, hours=1):
        return list(self.hot)

    def get_product_sales(self, period_key=None, hours=1):
        return list(self.sales)


@contextlib.contextmanager
def _patch_mysql(fake):
    old = sys.modules.get("mysql_db")
    sys.modules["mysql_db"] = fake
    try:
        yield fake
    finally:
        if old is None:
            sys.modules.pop("mysql_db", None)
        else:
            sys.modules["mysql_db"] = old


# ---------------- 纯函数：_diagnose ----------------

def test_diagnose_four_quadrants():
    assert sa._diagnose(0, 0, 0.0, 0.0)[0] == "cold"
    assert sa._diagnose(0, 5, 0.0, 0.0)[0] == "low_heat_high_sales"
    assert sa._diagnose(10, 0, 50.0, 40.0)[0] == "high_heat_no_sales"
    assert sa._diagnose(120, 8, 88.0, 50.0)[0] == "high_heat_low_sales"
    assert sa._diagnose(90, 35, 78.0, 40.0)[0] == "healthy"


def test_diagnose_calls_it_piece_per_visit_when_over_100_percent():
    quadrant, diagnosis, _ = sa._diagnose(10, 45, 80.0, 40.0)
    assert quadrant == "healthy"
    assert "件/人比 450%" in diagnosis
    assert "转化率" not in diagnosis, "转化率不可能 >100%"


# ---------------- 端到端：compare_hotness_vs_sales ----------------

def test_four_quadrant_output_and_summary():
    with _patch_mysql(_FakeMySQL(HOT, SALES, sources=["pos"])):
        out = sa.compare_hotness_vs_sales()

    zones = {z["zone_id"]: z for z in out["zones"]}
    assert zones["shelf_A"]["quadrant"] == "high_heat_low_sales"
    assert zones["shelf_A"]["avg_dwell_seconds"] == 50.0
    assert zones["shelf_A"]["conversion_rate"] == 6.7
    assert zones["shelf_B"]["quadrant"] == "low_heat_high_sales"
    assert zones["shelf_B"]["avg_dwell_seconds"] == 16.7
    assert zones["shelf_B"]["conversion_rate"] == 75.0
    assert zones["shelf_C"]["quadrant"] == "healthy"
    assert zones["shelf_C"]["conversion_note"] == "", "未超过 100% 时不追加口径说明"
    assert out["sales_source"] == "pos"
    assert out["data_caveat"] == "", "已确认真实 POS 数据时无需提示（唯一允许空串的情形）"
    assert "发现转化异常区域" in out["summary"]
    assert "1号货架" in out["summary"] and "2号货架" in out["summary"]
    assert "3号货架" not in out["summary"]


def test_over_100_percent_adds_conversion_note():
    hot = [{"zone_id": "shelf_A", "zone_label": "1号货架", "visit_count": 10,
            "total_dwell_seconds": 400, "heat_score": 80}]
    sales = [{"zone_id": "shelf_A", "sold_count": 45, "sales_amount": 500.0}]
    with _patch_mysql(_FakeMySQL(hot, sales, sources=["pos"])):
        out = sa.compare_hotness_vs_sales()

    zone = out["zones"][0]
    assert zone["conversion_rate"] == 450.0
    assert "件/人比" in zone["conversion_note"]
    assert "45 件 / 10 人次" in zone["conversion_note"]
    assert "件/人比" in zone["diagnosis"]


def test_simulated_source_gets_non_empty_caveat():
    with _patch_mysql(_FakeMySQL(HOT, SALES, sources=["simulated"])):
        out = sa.compare_hotness_vs_sales()
    assert out["sales_source"] == "simulated"
    assert "演示数据" in out["data_caveat"]


def test_missing_source_column_falls_back_to_period_key_prefix():
    fake = _FakeMySQL(HOT, SALES, sources=[])
    fake.sources_error = True
    fake.period_keys = ["demo"]
    with _patch_mysql(fake):
        out = sa.compare_hotness_vs_sales()
    assert out["sales_source"] == "simulated"
    assert "演示数据" in out["data_caveat"]


def test_source_detection_failure_is_not_silent():
    """判定失败 → unknown，且提示必须非空（否则与真实数据无法区分）。"""
    with _patch_mysql(_LegacyMySQL(HOT, SALES)):
        out = sa.compare_hotness_vs_sales()
    assert out["sales_source"] == "unknown"
    assert out["data_caveat"] != ""
    assert "无法确认" in out["data_caveat"]


def test_no_sales_means_summary_says_so():
    with _patch_mysql(_FakeMySQL(HOT, [], sources=[])):
        out = sa.compare_hotness_vs_sales()
    assert out["sales_source"] == "none"
    assert "没有销量记录" in out["data_caveat"]
    assert "未录入任何销量数据" in out["summary"]
    assert all(z["quadrant"] == "high_heat_no_sales" for z in out["zones"])
