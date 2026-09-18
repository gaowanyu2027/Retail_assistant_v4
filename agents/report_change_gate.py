"""变化门控：**没有变化就别调 LLM**（台账 F5 的成本治理）。

**问题**：定时汇报固定 10 分钟一轮 = **144 次 LLM 调用/天/店**（实测单次约 317+94 tokens、
1.4s —— 见 `/api/metrics/llm`）。夜里没人、指标一动不动的时候，这 144 次里有相当比例
只是在把同样的数字重新措辞一遍 ✗。

**做法**：汇报前先算一个**业务签名**（只含有意义的业务量：各区域到访/深度兴趣/停留、
告警数、表情样本数、数据可信度），与上一次汇报时的签名比较：

- 有变化（超过阈值）→ 正常调 LLM ✓
- 没变化 → **走模板降级分支**（不调 LLM），仍产出文字并入库 ✓（界面不会突然没消息）
- 几个**必须汇报**的例外（不能被门控吞掉）：首次、**数据可信度发生变化**（突然不可信/恢复）、
  异常突增（`force`）、以及**静默太久的心跳**（超过 `REPORT_MAX_SILENT_SECONDS`）✓

设计原则与项目其它地方一致：**宁可多报一次，也不要把"该说的事"咽掉** ——
所以"不可信↔可信"的翻转、突增、心跳都是硬性放行。
"""
from __future__ import annotations

import threading
import time

MAX_SILENT_SECONDS = 3600          # 静默心跳上限（秒）：超时必须真汇报一次
MIN_DELTA = 1                      # 业务量变化阈值（次数类指标，≥1 即算变化）


def signature(pop: dict | None, anom: dict | None, emo: dict | None,
              quality: dict | None = None) -> dict:
    """把"业务上值得汇报的变化"抽成一个可比对的签名（纯函数）。

    ⚠ 只取**业务量**，不取时间戳/内部状态 —— 否则每轮都"有变化"，门控形同虚设。
    """
    zones = {}
    for zid, z in ((pop or {}).get("zones") or {}).items():
        zones[zid] = [
            int(z.get("visit_count") or 0),
            int(z.get("deep_interest_count") or 0),
            round(float(z.get("total_dwell_seconds") or 0)),
        ]
    return {
        "zones": zones,
        "visitors": int((pop or {}).get("total_visitors") or 0),
        "visits": int((pop or {}).get("total_visits") or 0),
        "alerts": int((anom or {}).get("total") or 0),
        "alerts_high": len((anom or {}).get("high_risk") or []),
        "faces": int((emo or {}).get("total_faces") or 0),
        "negatives": int((emo or {}).get("negative_count") or 0),
        # 可信度是"该说的事"：翻转必须汇报，所以纳入签名
        "trusted": _trust_flag(quality),
    }


def _trust_flag(quality: dict | None) -> str:
    """把可信度快照压成三态：ok / degraded / unknown（纯函数，便于测试）。"""
    if not isinstance(quality, dict) or not quality:
        return "unknown"
    if quality.get("trustworthy") is False:
        return "degraded"
    if quality.get("trustworthy") is True:
        return "ok"
    # 兼容其它字段命名（不同调用方给的结构略有差异）
    if quality.get("fresh") is False or quality.get("stale") is True:
        return "degraded"
    return "ok"


def diff(prev: dict | None, cur: dict, min_delta: int = MIN_DELTA) -> list[str]:
    """返回"发生了哪些变化"的人话列表（空列表=没变化）。纯函数。"""
    if not prev:
        return ["首次汇报（无历史签名可比）"]
    reasons: list[str] = []
    if prev.get("trusted") != cur.get("trusted"):
        reasons.append(f"数据可信度变化：{prev.get('trusted')} → {cur.get('trusted')}")
    for key in ("visitors", "visits", "alerts", "alerts_high", "faces", "negatives"):
        a, b = int(prev.get(key) or 0), int(cur.get(key) or 0)
        if abs(b - a) >= min_delta:
            reasons.append(f"{key} {a} → {b}")
    zp, zc = prev.get("zones") or {}, cur.get("zones") or {}
    for zid in sorted(set(zp) | set(zc)):
        a, b = zp.get(zid) or [0, 0, 0], zc.get(zid) or [0, 0, 0]
        if any(abs(int(x or 0) - int(y or 0)) >= min_delta for x, y in zip(a, b)):
            reasons.append(f"{zid} {a} → {b}")
    return reasons


def _categorize(reason: str | None) -> str:
    """把判定原因归类成有界类别（用于累计统计，纯函数）。"""
    r = reason or "unknown"
    if r.startswith("首次"):
        return "first_report"
    if r.startswith("强制"):
        return "forced"
    if "心跳" in r:
        return "heartbeat"
    if "可信度" in r:
        return "trust_flip"
    if "无变化" in r:
        return "no_change"
    return "changed"


class ReportChangeGate:
    """记住上一次签名，决定"这轮要不要真的调 LLM"（线程安全）。"""

    def __init__(self, min_delta: int = MIN_DELTA, max_silent_seconds: float = MAX_SILENT_SECONDS):
        self.min_delta = min_delta
        self.max_silent_seconds = max_silent_seconds
        self._lock = threading.Lock()
        self._last_sig: dict | None = None
        self._last_llm_at: float | None = None
        self.reset_stats()

    def reset_stats(self) -> None:
        with self._lock:
            self.called = 0
            self.skipped = 0
            self.last_skip_reason: str | None = None
            self.skip_reasons: dict[str, int] = {}

    def snapshot(self) -> dict:
        with self._lock:
            total = self.called + self.skipped
            return {
                "called": self.called,
                "skipped": self.skipped,
                "skip_ratio": round(self.skipped / total, 3) if total else 0.0,
                "skip_reasons": dict(self.skip_reasons),
                "last_skip_reason": self.last_skip_reason,
                "min_delta": self.min_delta,
                "max_silent_seconds": self.max_silent_seconds,
            }

    def decide(self, sig: dict, *, force: bool = False, now: float | None = None) -> tuple[bool, str]:
        """返回 (是否要调 LLM, 原因)。**只做判断，不改状态** —— 便于离线测试。"""
        now = time.time() if now is None else now
        if force:
            return True, "强制汇报（异常突增等）"
        if self._last_sig is None:
            return True, "首次汇报"
        if self._last_llm_at is not None and (now - self._last_llm_at) >= self.max_silent_seconds:
            return True, f"静默超过 {int(self.max_silent_seconds)} 秒，心跳汇报"
        reasons = diff(self._last_sig, sig, self.min_delta)
        if reasons:
            return True, "；".join(reasons[:3])
        return False, "与上次汇报相比无变化"

    def commit(self, sig: dict, *, called_llm: bool, reason: str | None = None,
               now: float | None = None) -> None:
        """记录本轮结果。只有**真的调了 LLM** 才刷新心跳时间（否则心跳永不触发）。"""
        now = time.time() if now is None else now
        with self._lock:
            self._last_sig = sig
            if called_llm:
                self.called += 1
                self._last_llm_at = now
            else:
                self.skipped += 1
                self.last_skip_reason = reason
                # 归类成**有界**的类别：原因串里会带具体数字（如 "visits 3 → 4"），
                # 直接拿它当字典 key 会让 skip_reasons 无限增长 ✗
                cat = _categorize(reason)
                self.skip_reasons[cat] = self.skip_reasons.get(cat, 0) + 1


_GATE = ReportChangeGate()


def get_report_gate() -> ReportChangeGate:
    return _GATE
