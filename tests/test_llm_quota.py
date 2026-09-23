"""LLM 日配额（成本护栏）的判定逻辑与"入口都装了闸门"的静态守卫（2026-09-23）。

## 背景（为什么有这个东西）

公网可访问的问答接口每次调用都要花 token，而**模型平台没有"单 Key 额度上限"这类硬开关**
（控制台只提供"按 Key 查看用量"）。所以钱只能自己兜：账户少留余额 + 应用层按日限次。
默认两个上限都是 **0 = 不限**（不影响本机开发），演示前用 env 打开。
"""
import io
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in str(Path(__file__).resolve()):
    pass

QUERY_PY = PROJECT_ROOT / "api" / "routes" / "query.py"


def test_quota_disabled_always_allows():
    """两个上限都为 0（默认）= 不限 → 任何用量都放行。"""
    from agents.llm_quota import check_quota, quota_enabled

    assert quota_enabled(0, 0) is False
    for used in (0, 10, 10_000):
        ok, reason = check_quota(used, used, 0, 0)
        assert ok is True and reason == ""


def test_per_user_limit_boundary():
    """每账号 20 次：第 20 次放行、第 21 次拒绝（计数是"已用掉"的次数）。"""
    from agents.llm_quota import check_quota

    ok, _ = check_quota(19, 0, 20, 0)          # 已经问了 19 次 → 这是第 20 次
    assert ok is True, "第 20 次应该放行"
    ok, reason = check_quota(20, 0, 20, 0)     # 已经问了 20 次 → 第 21 次
    assert ok is False and "20" in reason, f"第 21 次应被拒且说明上限：{reason!r}"


def test_global_limit_boundary_and_message():
    """全站 200 次：到量即拒，且提示里带上限数字（前端要能给用户解释）。"""
    from agents.llm_quota import check_quota

    assert check_quota(0, 199, 0, 200)[0] is True
    ok, reason = check_quota(0, 200, 0, 200)
    assert ok is False and "200" in reason


def test_per_user_checked_before_global():
    """两个维度同时到量时，先报"账号额度"（对用户更可操作）。"""
    from agents.llm_quota import check_quota

    ok, reason = check_quota(20, 200, 20, 200)
    assert ok is False and "账号" in reason, f"应优先提示账号维度：{reason!r}"


def test_quota_gate_is_installed_on_every_query_entry():
    """两个主入口都必须调用闸门；兼容别名是转调它们，因此自动继承。

    这条守的是"以后新增问答入口忘了装闸门"——那会让成本护栏形同虚设（默认关闭时看不出来）。
    """
    src = io.open(QUERY_PY, encoding="utf-8").read()
    assert "_llm_quota_guard(owner)" in src, "配额闸门没有被调用"
    # 闸门调用次数应 ≥ 2（create_query / create_query_stream）
    assert len(re.findall(r"await _llm_quota_guard\(owner\)", src)) >= 2, \
        "两个主入口都必须在 write_owner() 之后调用配额闸门"
    # 兼容别名必须是**转调**，不能各写一份实现（否则闸门会漏）
    for alias, target in (("/query", "create_query"), ("/query/stream", "create_query_stream")):
        m = re.search(r'@router\.post\("' + re.escape(alias) + r'"[^)]*\)\s*async def (\w+)\(.*?\n\s+return await ' + target + r"\(", src, re.S)
        assert m, f"兼容别名 {alias} 不再是转调 {target}()（闸门会漏）"
