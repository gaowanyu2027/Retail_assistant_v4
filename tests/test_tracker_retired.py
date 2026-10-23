"""轨迹退役队列的有界化（慢泄漏回归）。

`cv_engine/multi_stream.py` 的多路常驻引擎只调 `process_frame`、从不 drain，
退役队列会随客流线性增长（实测 5000 条 ≈ 1.4MB）。这里钉住：

- `_retired` 最多保留 RETIRED_MAX=1000 条，超出丢最旧并计入 `retired_dropped`
- `drain_retired()` 取走并清空；之后仍会继续有界
- `max_lost` 容忍语义：`lost_times > max_lost` 才退役
- `get_active()` 只看"最近有匹配"的轨迹
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cv_engine.tracker as tracker  # noqa: E402
from cv_engine.tracker import TrackStateManager  # noqa: E402


def test_retired_queue_is_bounded_and_drops_oldest():
    mgr = TrackStateManager(max_lost=0)
    for tid in range(1, 1501):
        mgr.get_or_create(tid, [0, 0, 10, 10])
        mgr.mark_all_lost()
        mgr.cleanup()

    assert len(mgr._retired) == tracker.RETIRED_MAX == 1000
    assert mgr.retired_dropped == 500
    drained = mgr.drain_retired()
    assert len(drained) == 1000
    assert drained[0].track_id == 501, "丢弃的是最旧的 500 条"
    assert drained[-1].track_id == 1500
    assert mgr._retired == []


def test_lost_tolerance_boundary_and_active_view():
    mgr = TrackStateManager(max_lost=2)
    st = mgr.get_or_create(7, [0, 0, 10, 10])
    assert st.hit_times == 1 and st.lost_times == 0

    same = mgr.get_or_create(7, [0, 0, 20, 20])
    assert same is st
    assert st.hit_times == 2
    assert st.lost_times == 0
    assert st.last_center == (10.0, 10.0)

    mgr.mark_all_lost()
    mgr.mark_all_lost()
    mgr.cleanup()
    assert mgr.get(7) is st, "lost == max_lost 时仍保留（严格大于才退役）"
    assert mgr.active_count == 0, "连续未匹配不算活跃"

    mgr.mark_all_lost()
    mgr.cleanup()
    assert mgr.get(7) is None
    assert [t.track_id for t in mgr._retired] == [7]


def test_reset_clears_active_and_retired():
    mgr = TrackStateManager(max_lost=0)
    mgr.get_or_create(1, [0, 0, 10, 10])
    mgr.mark_all_lost()
    mgr.cleanup()
    assert len(mgr._retired) == 1

    mgr.reset()
    assert mgr._tracks == {}
    assert mgr._retired == []
    assert mgr.get(1) is None
