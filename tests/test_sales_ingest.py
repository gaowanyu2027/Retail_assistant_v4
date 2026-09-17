"""销量导入的时段边界与区域校验测试（台账 A9 / A14）。

- A9：补录历史批次只给 `period_key` 时，落库时间边界**不能默认成导入时刻**，
      否则"上周三 14 点"的销量会被算进"最近 1 小时"。
- A14：`shelf_a` 与 `shelf_A` 只差大小写 → 幽灵区域 → 四象限诊断反向。

两个被测函数都是纯函数（`period_bounds` / `validate_zone_ids`），离线可测；
端到端落库行为由实机验证覆盖。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.sales_ingest import (  # noqa: E402
    DEMO_PREFIX,
    period_bounds,
    validate_zone_ids,
)


# ---------------- A9：时段边界 ----------------

def test_hour_key_bounds():
    start, end = period_bounds("2026090314")
    assert start == "2026-09-03 14:00:00", start
    assert end == "2026-09-03 14:59:59", end


def test_minute_key_bounds():
    start, end = period_bounds("202609031430")
    assert start == "2026-09-03 14:30:00", start
    assert end == "2026-09-03 14:30:59", end


def test_bounds_never_default_to_now():
    """补录历史批次时推导出的时段必须是**历史**时段（不是当前时刻）。"""
    from datetime import datetime

    start, _ = period_bounds("2020010100")
    assert start.startswith("2020-01-01"), start
    assert start < datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def test_demo_and_invalid_keys_have_no_bounds():
    """演示命名空间与非法 key 都不推导（演示数据本就没有真实时段）。"""
    for pk in (f"{DEMO_PREFIX}traffic_2026090314", "2026-09-03", "abc", "", None, "202609031"):
        assert period_bounds(pk) is None, pk


def test_impossible_date_is_rejected():
    """形如 2026130100（13 月）要返回 None 而不是抛异常。"""
    assert period_bounds("2026130100") is None


# ---------------- A14：区域校验 ----------------

def test_case_mismatch_is_caught_with_suggestion():
    bad = validate_zone_ids(["shelf_a"], {"shelf_A", "shelf_B"})
    assert len(bad) == 1, bad
    zid, why = bad[0]
    assert zid == "shelf_a"
    assert "仅大小写不同" in why and "shelf_A" in why, why


def test_unknown_zone_is_caught():
    bad = validate_zone_ids(["shelf_Z"], {"shelf_A", "shelf_B"})
    assert len(bad) == 1 and bad[0][0] == "shelf_Z", bad
    assert "不在已配置区域" in bad[0][1], bad


def test_valid_zones_pass():
    assert validate_zone_ids(["shelf_A", "shelf_B"], {"shelf_A", "shelf_B"}) == []


def test_no_reference_means_no_rejection():
    """ROI 还没配置时不能凭空拒绝（否则销量完全导不进来）。"""
    assert validate_zone_ids(["anything"], set()) == []


def test_mixed_valid_and_invalid_reports_only_bad_ones():
    bad = validate_zone_ids(["shelf_A", "shelf_a", "checkout"], {"shelf_A", "checkout"})
    assert [z for z, _ in bad] == ["shelf_a"], bad
