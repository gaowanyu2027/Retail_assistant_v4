"""热度数据来源标注测试（台账 A8）。

被修的错误：`retail_stats`（视频热度）以前**没有来源字段**，而演示数据
（`seed_traffic_demo` / `simulate_demo_sales` 写入的 `demo*` 数据）与真实采集混在同一张表，
时段分析据此给出的排班/陈列建议可能建立在假数据上；销量侧早就有 `source`，热度侧没有。

修法对齐销量侧的既有约定：**标注而非静默过滤** ——
`hourly_traffic` / `zone_depth` 在结论里点明混入了多少条非真实数据，
同时提供 `source="video"` 只看真实采集。

这里只测纯函数部分（`source_breakdown`）；SQL 路径由实机验证覆盖。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.traffic_analytics import (  # noqa: E402
    REAL_SOURCE,
    hourly_traffic,
    source_breakdown,
    zone_depth,
)


def test_all_real_has_no_warning():
    br = source_breakdown({REAL_SOURCE: 30})
    assert br["all_real"] is True, br
    assert br["non_real_rows"] == 0, br
    assert br["note"] == "", br
    assert br["real_rows"] == 30 and br["total_rows"] == 30


def test_mixed_data_is_flagged_in_note():
    """混入演示数据必须**显式标注**，且点明不能作为经营决策依据。"""
    br = source_breakdown({REAL_SOURCE: 10, "simulated": 5, "test": 2})
    assert br["all_real"] is False, br
    assert br["real_rows"] == 10 and br["non_real_rows"] == 7, br
    assert "7 条非真实采集数据" in br["note"], br
    assert "不可作为经营决策依据" in br["note"], br
    # 具体来源名要出现，便于排查是谁写进去的
    assert "simulated" in br["note"] and "test" in br["note"], br


def test_selected_source_reports_exclusions():
    """显式过滤到 video 时，要说明"另有多少条被排除"，而不是假装它们不存在。"""
    br = source_breakdown({REAL_SOURCE: 4, "simulated": 6}, selected=REAL_SOURCE)
    assert br["selected_source"] == REAL_SOURCE, br
    assert "仅统计 source=video" in br["note"], br
    assert "6 条非真实采集数据被排除" in br["note"], br


def test_selected_source_with_nothing_excluded():
    br = source_breakdown({REAL_SOURCE: 4}, selected=REAL_SOURCE)
    assert "排除" not in br["note"], br


def test_empty_breakdown_is_safe():
    br = source_breakdown({})
    assert br["total_rows"] == 0 and br["all_real"] is True, br
    assert br["note"] == "", br


def test_none_values_tolerated():
    """历史数据/脏数据里 source 可能是 NULL，不能因此抛异常。"""
    br = source_breakdown({None: 3, "video": 2})
    assert br["non_real_rows"] == 3, br
    assert "unknown" in br["note"], br


def test_read_paths_expose_source_filter():
    """两个时段分析入口都必须提供 source 参数（否则无法只看真实采集）。"""
    import inspect

    for fn in (hourly_traffic, zone_depth):
        params = inspect.signature(fn).parameters
        assert "source" in params, f"{fn.__name__} 缺少 source 参数"
        assert params["source"].default is None, f"{fn.__name__} 的 source 默认应为 None（不过滤但标注）"
