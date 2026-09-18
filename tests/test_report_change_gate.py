"""变化门控测试（台账 F5：无变化就别调 LLM）。

被测对象是**纯判断逻辑**（签名 / 差异 / 决定），离线可测：
- 没变化 → 跳过（不调 LLM，省一次 token）
- 业务量变化 → 调
- **数据可信度翻转** → 必须调（"该说的事"不能被门控吞掉）
- 异常突增（force / report_type=surge）→ 必须调
- 静默太久 → 心跳汇报（避免用户长时间看不到消息）
- 只有**真的调了 LLM** 才刷新心跳时间（否则心跳永不触发）
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.report_change_gate import ReportChangeGate, diff, signature  # noqa: E402


def _pop(visits=0, visitors=0, dwell=0, zone="shelf_A"):
    return {"zones": {zone: {"visit_count": visits, "deep_interest_count": 0,
                             "total_dwell_seconds": dwell}},
            "total_visitors": visitors, "total_visits": visits}


def test_signature_ignores_noise_but_keeps_business_numbers():
    sig = signature(_pop(visits=3, visitors=2), {"total": 1, "high_risk": []},
                    {"total_faces": 5, "negative_count": 1}, {"trustworthy": True})
    assert sig["visits"] == 3 and sig["visitors"] == 2, sig
    assert sig["alerts"] == 1 and sig["faces"] == 5, sig
    assert sig["trusted"] == "ok", sig


def test_first_report_always_calls_llm():
    gate = ReportChangeGate()
    ok, reason = gate.decide(signature(_pop(), {}, {}, None))
    assert ok is True and "首次" in reason, reason


def test_no_change_is_skipped():
    """核心：同样的业务量 → 第二轮跳过 LLM。"""
    gate = ReportChangeGate()
    sig = signature(_pop(visits=3, visitors=2), {"total": 0}, {"total_faces": 0, "negative_count": 0},
                    {"trustworthy": True})
    assert gate.decide(sig)[0] is True          # 首次
    gate.commit(sig, called_llm=True)
    ok, reason = gate.decide(sig)
    assert ok is False and "无变化" in reason, reason


def test_visit_change_triggers_llm():
    gate = ReportChangeGate()
    a = signature(_pop(visits=3, visitors=2), {}, {}, {"trustworthy": True})
    gate.commit(a, called_llm=True)
    b = signature(_pop(visits=4, visitors=3), {}, {}, {"trustworthy": True})
    ok, reason = gate.decide(b)
    assert ok is True and "visits" in reason, reason


def test_trust_flip_always_reported():
    """数据从"可信"变"不可信"（或反过来）必须汇报 —— 这是该说的事。"""
    gate = ReportChangeGate()
    ok_sig = signature(_pop(), {}, {}, {"trustworthy": True})
    gate.commit(ok_sig, called_llm=True)
    bad_sig = signature(_pop(), {}, {}, {"trustworthy": False})
    ok, reason = gate.decide(bad_sig)
    assert ok is True and "可信度" in reason, reason


def test_surge_forces_llm_even_without_change():
    """异常突增：即使业务量签名没变也必须汇报（安全优先）。"""
    gate = ReportChangeGate()
    sig = signature(_pop(), {}, {}, {"trustworthy": True})
    gate.commit(sig, called_llm=True)
    ok, reason = gate.decide(sig, force=True)
    assert ok is True and "强制" in reason, reason


def test_heartbeat_after_long_silence():
    """静默超过上限 → 心跳汇报（不能因为"没变化"就永远不说话）。"""
    gate = ReportChangeGate(max_silent_seconds=100)
    sig = signature(_pop(), {}, {}, {"trustworthy": True})
    gate.commit(sig, called_llm=True, now=1000.0)
    assert gate.decide(sig, now=1050.0)[0] is False          # 还没到心跳
    ok, reason = gate.decide(sig, now=1200.0)
    assert ok is True and "心跳" in reason, reason


def test_skip_does_not_refresh_heartbeat():
    """跳过的那一轮不能刷新心跳时间，否则永远等不到心跳。"""
    gate = ReportChangeGate(max_silent_seconds=100)
    sig = signature(_pop(), {}, {}, {"trustworthy": True})
    gate.commit(sig, called_llm=True, now=1000.0)
    gate.commit(sig, called_llm=False, now=1500.0)            # 跳过
    ok, reason = gate.decide(sig, now=1500.0)
    assert ok is True and "心跳" in reason, reason


def test_stats_track_skip_ratio_and_reasons():
    gate = ReportChangeGate()
    sig = signature(_pop(visits=1), {}, {}, {"trustworthy": True})
    gate.commit(sig, called_llm=True, reason="首次汇报")
    for _ in range(3):
        ok, reason = gate.decide(sig)
        assert ok is False
        gate.commit(sig, called_llm=False, reason=reason)     # 真实调用就是这么传的
    s = gate.snapshot()
    assert s["called"] == 1 and s["skipped"] == 3, s
    assert s["skip_ratio"] == 0.75, s
    assert "与上次汇报相比无变化" in s["last_skip_reason"], s
    # 原因要归类成**有界**类别（原因串带数字，直接当 key 会无限增长）
    assert s["skip_reasons"] == {"no_change": 3}, s


def test_diff_reports_zone_level_changes():
    prev = signature(_pop(visits=1, dwell=10), {}, {}, {"trustworthy": True})
    cur = signature(_pop(visits=1, dwell=99), {}, {}, {"trustworthy": True})
    reasons = diff(prev, cur)
    assert any("shelf_A" in r for r in reasons), reasons


def test_trust_flag_handles_unknown_shapes():
    assert signature({}, {}, {}, None)["trusted"] == "unknown"
    assert signature({}, {}, {}, {})["trusted"] == "unknown"
    assert signature({}, {}, {}, {"fresh": False})["trusted"] == "degraded"
