"""表情趋势判定测试（台账 A19）。

被修的错误：只看**总**样本数（10）就下方向性结论 —— 10 个样本劈成两半各 5 个，
"9 个 neutral + 1 个 sad" 也能判成"顾客情绪明显下降"。

现在两条门槛：前后半段各自 ≥ `EMOTION_TREND_MIN_PER_HALF`，且两段正向率之差
要通过两比例 z 检验（|z| ≥ `EMOTION_TREND_Z_THRESHOLD`）。

测试走**公开 API**（`process()` 喂表情 → `get_trend()` 取结论），
用鸭子类型的假轨迹即可，不需要模型/摄像头/数据库。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    EMOTION_TREND_MIN_PER_HALF,
    EMOTION_TREND_Z_THRESHOLD,
)
from skills.skill_emotion import SkillEmotion  # noqa: E402


def _feed(skill, emotions):
    """每条样本都用一个"新顾客"（新对象 = 新轨迹），逐条喂进技能。"""
    for i, emo in enumerate(emotions):
        face = SimpleNamespace(track_id=i, emotion=emo, emotion_conf=0.9,
                               _last_counted_emotion=None)
        skill.process([face], timestamp=float(i))


def _half_min():
    return EMOTION_TREND_MIN_PER_HALF


# ---------------- 台账 A19 的原始场景 ----------------

def test_nine_neutral_one_sad_is_not_a_trend():
    """9 neutral + 1 sad（原实现报"明显下降"）→ 现在必须承认数据不足。"""
    skill = SkillEmotion()
    _feed(skill, ["neutral"] * 9 + ["sad"])
    t = skill.get_trend()
    assert t["trend"] == "not_enough_data", t
    assert t["significant"] is False, t
    assert "数据不足" in t["conclusion"], t
    assert t["min_per_half"] == _half_min(), t


def test_small_sample_with_big_difference_still_refuses():
    """样本少但差异大（6 neutral vs 6 sad）也不能下结论 —— 这正是"看起来很明显"的陷阱。"""
    skill = SkillEmotion()
    _feed(skill, ["neutral"] * 6 + ["sad"] * 6)
    t = skill.get_trend()
    assert t["trend"] == "not_enough_data", t


# ---------------- 够样本时的显著性 ----------------

def test_single_negative_outlier_is_not_significant():
    """样本够了（前半 20 全正向，后半 19 正向 + 1 sad）→ 差异不显著，不能报下降。"""
    skill = SkillEmotion()
    n = _half_min()
    _feed(skill, ["neutral"] * n + ["neutral"] * (n - 1) + ["sad"])
    t = skill.get_trend()
    assert t["early_count"] >= n and t["late_count"] >= n, t
    assert t["significant"] is False, t
    assert t["trend"] == "flat", t
    assert abs(t["z"]) < EMOTION_TREND_Z_THRESHOLD, t
    assert "波动" in t["conclusion"] or "稳定" in t["conclusion"], t


def test_real_drop_is_flagged_and_marked_significant():
    """真下降（前半全正向、后半全负向）必须被判为显著下降。"""
    skill = SkillEmotion()
    n = _half_min()
    _feed(skill, ["neutral"] * n + ["sad"] * n)
    t = skill.get_trend()
    assert t["significant"] is True, t
    assert t["trend"] == "down", t
    assert "明显下降" in t["conclusion"], t
    assert t["delta"] <= -0.5, t


def test_identical_halves_are_flat_without_division_by_zero():
    """两段完全相同 → delta=0（se 也≈0，不能出现除零/NaN）。"""
    skill = SkillEmotion()
    n = _half_min()
    _feed(skill, ["neutral"] * (2 * n))
    t = skill.get_trend()
    assert t["delta"] == 0, t
    assert t["z"] == 0.0, t
    assert t["significant"] is False, t
    assert "无明显变化" in t["conclusion"], t


def test_trend_key_present_in_both_branches():
    """`trend` 字段必须两个分支都有（原实现只在"数据不足"分支有，正常分支没有）。"""
    empty = SkillEmotion()
    assert "trend" in empty.get_trend(), empty.get_trend()
    full = SkillEmotion()
    n = _half_min()
    _feed(full, ["happy"] * (2 * n))
    assert "trend" in full.get_trend(), full.get_trend()


def test_improvement_is_flagged():
    """反过来：情绪明显好转也要报出来（别只防"下降"）。"""
    skill = SkillEmotion()
    n = _half_min()
    _feed(skill, ["sad"] * n + ["happy"] * n)
    t = skill.get_trend()
    assert t["significant"] is True and t["trend"] == "up", t
    assert "好转" in t["conclusion"], t


def test_low_confidence_faces_are_ignored():
    """置信度低于阈值的样本不进时间线（否则噪声会污染趋势结论）。"""
    skill = SkillEmotion()
    for i in range(_half_min() * 2):
        face = SimpleNamespace(track_id=i, emotion="sad", emotion_conf=0.05,
                               _last_counted_emotion=None)
        skill.process([face], timestamp=float(i))
    t = skill.get_trend()
    assert t["trend"] == "not_enough_data", t
