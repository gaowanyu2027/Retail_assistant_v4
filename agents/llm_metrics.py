"""LLM 调用指标（Agent 侧可观测性）。

**为什么需要**：Agent 项目的成本与延迟都藏在 LLM 调用里 —— 没有指标就只能"感觉慢/感觉贵"。
本项目还实测过一个典型成本问题：定时汇报**固定 10 分钟一轮 = 144 次/天/店**（优化项 F5），
但当时**没有任何数据能证明它花了多少** —— 这个模块就是为了让这类判断有数字。

**怎么收口**：全项目有 9 处 LLM 调用，但**只有一个创建入口** `agents/base_agent.create_llm()`。
所以这里用 LangChain 的 callback 机制（`on_llm_end` 能拿到 `usage_metadata`）挂一次，全覆盖 ✓
—— 不需要在每个调用点插代码（那才是容易漏的方式）。

**口径与诚实边界**：
- `tokens` 取自 LangChain 的 `usage_metadata`（服务端返回的真实用量）✓；取不到时该次调用**只计次数不计 token**（不猜）✓
- `cost_estimate` **只有配置了单价才算**（`LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK`）。
  默认不填 → 只报 token，**不编造价格** ✓（各家价格会变，写死一个数才是错的）
- 延迟样本有界（最近 N 次）→ 只报"最近窗口"的 p50/p95，长跑不会无限增长内存 ✓
"""
from __future__ import annotations

import threading
import time
from collections import Counter, deque

MAX_LATENCY_SAMPLES = 500


class LLMMetrics:
    """进程内 LLM 调用指标（线程安全）。"""

    def __init__(self, max_samples: int = MAX_LATENCY_SAMPLES):
        self._lock = threading.Lock()
        self._latencies: deque[float] = deque(maxlen=max_samples)
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.calls = 0
            self.errors = 0
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.calls_without_usage = 0          # token 信息缺失的调用（LangChain 未回传时）
            self.by_tag: Counter = Counter()
            self.by_model: Counter = Counter()
            self.tag_tokens: dict[str, list[int]] = {}
            self._latencies.clear()
            self.first_call_at: float | None = None
            self.last_call_at: float | None = None

    def record(self, *, tag: str = "unknown", model: str = "unknown",
               latency_ms: float = 0.0, prompt_tokens: int | None = None,
               completion_tokens: int | None = None, ok: bool = True) -> None:
        with self._lock:
            self.calls += 1
            if not ok:
                self.errors += 1
            self.by_tag[tag] += 1
            self.by_model[model] += 1
            self._latencies.append(max(0.0, latency_ms))
            now = time.time()
            if self.first_call_at is None:
                self.first_call_at = now
            self.last_call_at = now
            if prompt_tokens is None and completion_tokens is None:
                self.calls_without_usage += 1
                return
            pt = int(prompt_tokens or 0)
            ct = int(completion_tokens or 0)
            self.prompt_tokens += pt
            self.completion_tokens += ct
            slot = self.tag_tokens.setdefault(tag, [0, 0])
            slot[0] += pt
            slot[1] += ct

    @staticmethod
    def _percentile(sorted_vals: list[float], q: float) -> float:
        if not sorted_vals:
            return 0.0
        idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
        return round(sorted_vals[idx], 1)

    def snapshot(self, *, price_in_per_mtok: float = 0.0,
                 price_out_per_mtok: float = 0.0) -> dict:
        with self._lock:
            vals = sorted(self._latencies)
            cost = None
            if price_in_per_mtok or price_out_per_mtok:
                cost = round(
                    self.prompt_tokens / 1_000_000 * price_in_per_mtok
                    + self.completion_tokens / 1_000_000 * price_out_per_mtok, 6)
            window = 0.0
            if self.first_call_at and self.last_call_at:
                window = round(self.last_call_at - self.first_call_at, 1)
            return {
                "calls": self.calls,
                "errors": self.errors,
                "error_rate": round(self.errors / self.calls, 4) if self.calls else 0.0,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "calls_without_usage": self.calls_without_usage,
                "latency_ms": {
                    "samples": len(vals),
                    "p50": self._percentile(vals, 0.50),
                    "p95": self._percentile(vals, 0.95),
                    "max": round(vals[-1], 1) if vals else 0.0,
                    "avg": round(sum(vals) / len(vals), 1) if vals else 0.0,
                },
                "by_tag": dict(self.by_tag.most_common()),
                "tokens_by_tag": {k: {"prompt": v[0], "completion": v[1]}
                                  for k, v in self.tag_tokens.items()},
                "by_model": dict(self.by_model.most_common()),
                "window_seconds": window,
                # 成本只在配置了单价时给出；否则为 None（不编造价格）
                "cost_estimate": cost,
                "cost_currency": "USD" if cost is not None else None,
                "cost_note": (
                    "按配置单价估算（LLM_PRICE_IN_PER_MTOK / LLM_PRICE_OUT_PER_MTOK）；"
                    "未配置单价时只报 token 数" if cost is None else
                    f"按 输入 {price_in_per_mtok}/M、输出 {price_out_per_mtok}/M 估算"
                ),
            }


_METRICS = LLMMetrics()


def get_llm_metrics() -> LLMMetrics:
    return _METRICS


def make_metrics_handler(tag: str):
    """构造一个 LangChain callback handler，把每次 LLM 调用记进指标。

    用 callback 而不是改每个调用点：全项目 9 处调用、只有 1 处创建实例，
    挂在创建处才不会漏（漏一处，成本数字就是错的）。
    """
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except Exception:                     # 依赖缺失时退化为空 handler，绝不影响主链路
        return None

    class _Handler(BaseCallbackHandler):
        def __init__(self):
            super().__init__()
            self._t0: float | None = None

        def on_llm_start(self, serialized, prompts, **kwargs):
            self._t0 = time.perf_counter()

        def on_llm_end(self, response, **kwargs):
            latency = (time.perf_counter() - self._t0) * 1000 if self._t0 else 0.0
            self._t0 = None
            pt = ct = None
            model = "unknown"
            try:
                gen = (response.generations or [[]])[0][0]
                msg = getattr(gen, "message", None)
                usage = getattr(msg, "usage_metadata", None) if msg is not None else None
                if usage:
                    pt = usage.get("input_tokens")
                    ct = usage.get("output_tokens")
                    model = (usage.get("model_name")
                             or getattr(msg, "response_metadata", {}).get("model_name")
                             or model)
                if model == "unknown":
                    model = (getattr(msg, "response_metadata", {}) or {}).get("model_name", model)
            except Exception:
                pass
            _METRICS.record(tag=tag, model=model, latency_ms=latency,
                            prompt_tokens=pt, completion_tokens=ct, ok=True)

        def on_llm_error(self, error, **kwargs):
            latency = (time.perf_counter() - self._t0) * 1000 if self._t0 else 0.0
            self._t0 = None
            _METRICS.record(tag=tag, latency_ms=latency, ok=False)

    return _Handler()
