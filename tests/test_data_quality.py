"""数据可信度门禁：断流不能输出成"0 客流"的业务结论。

`agents/data_quality.py` 的 docstring 记着两个实测坑，这里逐条钉住：

1. 从未收到帧 → reason 明确说"视频源未启动"，而不是让统计值 0 冒充业务事实；
2. 顶层 `reliable` 是"任一路新鲜"的聚合口径，**业务结论必须用
   `reliable_by_source[src]`** —— 否则浏览器在推帧会把停摆的服务器摄像头粉饰成可信。

用假时钟推进时间（不 sleep、不碰真实视频源），结束后恢复模块状态。
"""
import contextlib
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import agents.data_quality as dq  # noqa: E402


class _Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def monotonic(self):
        return self.t


@contextlib.contextmanager
def _fake_clock(start=1000.0, stale_seconds=90):
    clock = _Clock(start)
    old_time = dq.time
    old_stale = dq.DATA_STALE_SECONDS
    dq.time = types.SimpleNamespace(monotonic=clock.monotonic)
    dq.DATA_STALE_SECONDS = stale_seconds
    dq.reset()
    try:
        yield clock
    finally:
        dq.time = old_time
        dq.DATA_STALE_SECONDS = old_stale
        dq.reset()


def test_no_frames_means_no_data_not_zero_business():
    with _fake_clock():
        snap = dq.snapshot()
        assert snap["reliable"] is False
        assert snap["sources"] == {}
        assert "视频源未启动" in snap["reason"]
        assert dq.warning_text().startswith("【数据不可信】")


def test_fresh_frame_is_reliable_and_counted():
    with _fake_clock():
        dq.mark_frame(dq.SOURCE_RETAIL)
        dq.mark_frame(dq.SOURCE_RETAIL)
        snap = dq.snapshot()
        assert snap["reliable"] is True
        assert snap["reliable_by_source"][dq.SOURCE_RETAIL] is True
        assert snap["sources"][dq.SOURCE_RETAIL]["frames"] == 2
        assert dq.warning_text(dq.SOURCE_RETAIL) == ""


def test_age_equal_to_threshold_is_still_fresh():
    with _fake_clock() as clock:
        dq.mark_frame(dq.SOURCE_RETAIL)
        clock.t += 90
        snap = dq.snapshot()
        assert snap["sources"][dq.SOURCE_RETAIL]["stale"] is False


def test_stale_source_is_unreliable_with_reason():
    with _fake_clock() as clock:
        dq.mark_frame(dq.SOURCE_RETAIL)
        clock.t += 91
        snap = dq.snapshot()
        assert snap["reliable"] is False
        assert "断流" in snap["reason"] and "91" in snap["reason"]
        assert dq.is_reliable(dq.SOURCE_RETAIL) is False


def test_aggregate_can_be_true_while_business_source_is_stale():
    """浏览器推帧不能把停摆的服务器摄像头粉饰成"数据可信"。"""
    with _fake_clock() as clock:
        dq.mark_frame(dq.SOURCE_RETAIL)
        clock.t += 91
        dq.mark_frame(dq.SOURCE_CLIENT)
        snap = dq.snapshot()
        assert snap["reliable"] is True                      # 聚合口径：任一路新鲜
        assert snap["reliable_by_source"][dq.SOURCE_RETAIL] is False
        assert snap["reliable_by_source"][dq.SOURCE_CLIENT] is True
        assert dq.is_reliable(dq.SOURCE_RETAIL) is False     # 业务口径：该源自身
        text = dq.warning_text(dq.SOURCE_RETAIL)
        assert "服务器摄像头" in text and "本机摄像头" in text
        assert "仍在推送" in text
        assert dq.warning_text(dq.SOURCE_CLIENT) == ""


def test_reset_clears_frames_and_sources():
    with _fake_clock():
        dq.mark_frame(dq.SOURCE_RETAIL)
        dq.reset()
        snap = dq.snapshot()
        assert snap["sources"] == {}
        assert snap["reliable"] is False
