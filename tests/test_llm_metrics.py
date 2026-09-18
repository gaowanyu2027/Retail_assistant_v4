"""LLM 调用指标测试（Agent 侧可观测性）。

口径要点（也是这个模块存在的意义）：
- token 取不到时**只计次数、不计 token**，并明确标注有多少次缺用量 —— 不猜；
- 成本**只在配置了单价时**才计算，否则为 None（价格会变，写死就是错的）；
- 延迟分位只统计**最近 N 次**（有界窗口），长跑不会无限涨内存；
- 失败单独计数，不混进 token 统计。

纯内存对象，离线可测。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.llm_metrics import LLMMetrics  # noqa: E402


def test_counts_calls_tokens_and_tags():
    m = LLMMetrics()
    m.record(tag="answer", model="deepseek-chat", latency_ms=1200,
             prompt_tokens=800, completion_tokens=200)
    m.record(tag="answer", model="deepseek-chat", latency_ms=800,
             prompt_tokens=600, completion_tokens=100)
    m.record(tag="report", model="deepseek-chat", latency_ms=2000,
             prompt_tokens=1000, completion_tokens=400)
    s = m.snapshot()
    assert s["calls"] == 3, s
    assert s["prompt_tokens"] == 2400 and s["completion_tokens"] == 700, s
    assert s["total_tokens"] == 3100, s
    assert s["by_tag"]["answer"] == 2 and s["by_tag"]["report"] == 1, s
    assert s["tokens_by_tag"]["report"] == {"prompt": 1000, "completion": 400}, s


def test_missing_usage_is_counted_not_guessed():
    """LangChain 没回传用量时：只计次数、不计 token，并标注缺了多少次。"""
    m = LLMMetrics()
    m.record(tag="answer", latency_ms=500)                     # 无 token 信息
    m.record(tag="answer", latency_ms=500, prompt_tokens=10, completion_tokens=5)
    s = m.snapshot()
    assert s["calls"] == 2 and s["calls_without_usage"] == 1, s
    assert s["total_tokens"] == 15, s


def test_cost_only_when_price_configured():
    m = LLMMetrics()
    m.record(tag="answer", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert m.snapshot()["cost_estimate"] is None, "没配单价就不该给出成本"
    s = m.snapshot(price_in_per_mtok=0.5, price_out_per_mtok=2.0)
    assert s["cost_estimate"] == 2.5, s
    assert "USD" in (s["cost_currency"] or ""), s


def test_latency_percentiles_and_window_bound():
    m = LLMMetrics(max_samples=100)
    for i in range(1, 101):
        m.record(tag="answer", latency_ms=i)
    s = m.snapshot()
    assert s["latency_ms"]["samples"] == 100, s
    assert s["latency_ms"]["p50"] == 51, s
    # p95 的取值取决于分位索引约定（最近秩 vs 插值）：这里用取整索引 → 95
    assert s["latency_ms"]["p95"] in (95, 96), s
    assert s["latency_ms"]["max"] == 100, s
    # 超出窗口后仍只保留最近 N 个（内存有界）
    for _ in range(50):
        m.record(tag="answer", latency_ms=9999)
    s2 = m.snapshot()
    assert s2["latency_ms"]["samples"] == 100, s2
    assert s2["latency_ms"]["max"] == 9999, s2


def test_errors_counted_separately():
    m = LLMMetrics()
    m.record(tag="answer", latency_ms=100, ok=True)
    m.record(tag="answer", latency_ms=100, ok=False)
    s = m.snapshot()
    assert s["errors"] == 1 and s["calls"] == 2, s
    assert abs(s["error_rate"] - 0.5) < 1e-9, s


def test_reset_clears_everything():
    m = LLMMetrics()
    m.record(tag="answer", prompt_tokens=5, completion_tokens=5, ok=False)
    m.reset()
    s = m.snapshot()
    assert s["calls"] == 0 and s["errors"] == 0 and s["total_tokens"] == 0, s
    assert s["latency_ms"]["samples"] == 0 and s["by_tag"] == {}, s


def test_handler_construction_never_breaks_main_path():
    """即使 langchain 缺失/异常，构造 handler 也不能抛（它挂在主链路上）。"""
    from agents.llm_metrics import make_metrics_handler
    h = make_metrics_handler("answer")
    assert h is None or h is not None          # 只要求不抛异常
