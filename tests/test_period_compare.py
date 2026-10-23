"""同期对比（昨天 / 上周）的核心口径。

钉住三件在台账里踩过的事：

1. **窗口必须等长**（A12）：本小时只过了 N 分钟时，历史基线也要截到同一分钟，
   否则 15:05 会显示 -93.3% 的假暴跌；
2. **取数失败 ≠ 没有数据**：数据库异常不能输出成"请检查摄像头/视频源"；
3. **销量是小时粒度、无法按分钟截断**，必须写进 note，不能冒充等长对比。

用假 `mysql_db` 模块（`_aggregate` 内部 `import mysql_db`），记录每次查询的
`until_key` 以断言窗口对齐；结束后恢复 `sys.modules`。
"""
import contextlib
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents import period_compare as pc  # noqa: E402


class _FakeMySQL:
    def __init__(self, stats=None, sales=None, fail_stats=False, fail_sales=False):
        self.stats = stats or {}
        self.sales = sales or {}
        self.fail_stats = fail_stats
        self.fail_sales = fail_sales
        self.calls: list[tuple] = []

    def get_retail_stats_by_zone(self, period_key, until_key=None):
        self.calls.append(("stats", period_key, until_key))
        if self.fail_stats:
            raise RuntimeError("Can't connect to MySQL server")
        return list(self.stats.get(period_key, []))

    def get_product_sales(self, period_key):
        self.calls.append(("sales", period_key, None))
        if self.fail_sales:
            raise RuntimeError("Can't connect to MySQL server")
        return list(self.sales.get(period_key, []))


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


# ---------------- 纯函数 ----------------

def test_period_key_is_hourly_10_digits():
    assert pc._period_key(datetime(2026, 10, 23, 15, 7, 30)) == "2026102315"


def test_pct_and_delta_boundaries():
    assert pc._pct(12, 10) == 20.0
    assert pc._pct(-5, 10) == -150.0
    assert pc._pct(3, 0) is None, "基数为 0 时变化率无意义"
    assert pc._delta(12, 10) == 2.0


def test_trend_text_flat_threshold_is_strict():
    assert pc._trend_text(None) == "无对比基线（基数为 0）"
    assert pc._trend_text(5.0) == "基本持平（5.0%）", "阈值边界算持平"
    assert pc._trend_text(5.1) == "上升 5.1%"
    assert pc._trend_text(-5.1) == "下降 5.1%"
    assert pc._trend_text(0.0) == "基本持平（0.0%）"


# ---------------- 窗口对齐（A12） ----------------

def test_compare_windows_align_to_same_minute():
    fake = _FakeMySQL()
    with _patch_mysql(fake):
        out = pc.compare_periods(datetime(2026, 10, 23, 15, 7, 30))

    assert fake.calls == [
        ("stats", "2026102315", "202610231507"),
        ("sales", "2026102315", None),
        ("stats", "2026102215", "202610221507"),
        ("sales", "2026102215", None),
        ("stats", "2026101615", "202610161507"),
        ("sales", "2026101615", None),
    ], "当前与昨天/上周同期都必须截到同一分钟"
    assert out["comparisons"][0]["period_key"] == "2026102215"
    assert out["comparisons"][1]["period_key"] == "2026101615"


def test_partial_hour_note_mentions_asymmetric_sales_window():
    with _patch_mysql(_FakeMySQL()):
        out = pc.compare_periods(datetime(2026, 10, 23, 15, 7))
    assert "第 7 分钟" in out["note"]
    assert "相同已过分钟数" in out["note"]
    assert "销量是小时粒度" in out["note"]


def test_no_partial_note_at_minute_55():
    fake = _FakeMySQL(stats={"2026102315": [{"visit_count": 1}]})
    with _patch_mysql(fake):
        out = pc.compare_periods(datetime(2026, 10, 23, 15, 55))
    assert out["note"] == ""


# ---------------- 取数失败 vs 没有数据 ----------------

def test_db_error_is_not_misdiagnosed_as_missing_camera():
    fake = _FakeMySQL(fail_stats=True)
    with _patch_mysql(fake):
        out = pc.compare_periods(datetime(2026, 10, 23, 15, 7))

    assert out["current"]["available"] is False
    assert "retail_stats(RuntimeError)" in out["current"]["data_error"]
    assert "product_sales" not in out["current"]["data_error"], "销量查询本身成功"
    assert "取数失败" in out["conclusion"]
    assert "不要据此判断摄像头/视频源有问题" in out["conclusion"]
    assert "数据不可用" in out["note"]


def test_empty_period_is_reported_as_no_data_not_db_error():
    with _patch_mysql(_FakeMySQL()):
        out = pc.compare_periods(datetime(2026, 10, 23, 15, 7))

    assert out["current"]["available"] is True
    assert out["current"]["has_data"] is False
    assert "没有采集到数据" in out["conclusion"]
    assert "摄像头/视频源" in out["conclusion"]
    assert "当前时段无数据" in out["note"]


# ---------------- 对比结论 ----------------

def test_comparison_values_and_conclusion():
    fake = _FakeMySQL(
        stats={
            "2026102315": [{"visit_count": 12, "total_dwell_seconds": 100.0}],
            "2026102215": [{"visit_count": 10, "total_dwell_seconds": 80.0}],
        },
        sales={"2026102315": [{"sold_count": 5, "sales_amount": 50.0}]},
    )
    with _patch_mysql(fake):
        out = pc.compare_periods(datetime(2026, 10, 23, 15, 7))

    assert out["current"]["visit_count"] == 12
    assert out["current"]["zone_count"] == 1
    yesterday = out["comparisons"][0]
    assert yesterday["visit_count"] == 10
    assert yesterday["visit_delta"] == 2
    assert yesterday["visit_pct"] == 20.0
    assert yesterday["sold_pct"] is None, "昨天没有销量 → 无基线"
    assert "较昨天同期到访上升 20.0%（10 → 12 人次）" in out["conclusion"]
    assert "上周同期无记录，缺少对比基线" in out["conclusion"]
