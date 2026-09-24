"""意图推断（`agents/intent_router.infer_intent_from_tools`）的离线测试（2026-09-24）。

背景（CI run#22 实测，唯一失败项 mem_04）：
    问题「帮我看看当前客流和告警情况」
    路径工具: ['get_hourly_traffic', 'get_anomaly_alerts', ...]
    期望意图: general/both ，实际: anomaly
根因：原逻辑只把 `shelf_popularity` 当"热度域"，客流类工具（hourly_traffic 等）没被算进任何域
→ "客流 + 告警"必然判成 anomaly；而标签随调用顺序变化，又让它在 CI 里时红时绿。

修法：先把工具映射到**数据域**，再按**域集合**判定 —— 与顺序无关。
"""
import io
import itertools
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.intent_router import infer_intent_from_tools  # noqa: E402


def test_popularity_plus_anomaly_is_both_any_order():
    """mem_04 的回归：客流 + 告警 = `both`，且**与调用顺序无关**（这是它时红时绿的根因）。"""
    tools = ["get_hourly_traffic", "get_anomaly_alerts"]
    for perm in itertools.permutations(tools):
        got = infer_intent_from_tools(list(perm))
        assert got == "both", f"顺序 {perm} 得到 {got}，应为 both"


def test_single_domain_intents():
    assert infer_intent_from_tools(["get_shelf_popularity"]) == "popularity"
    assert infer_intent_from_tools(["get_hourly_traffic"]) == "popularity"     # 客流也属"热度/经营数据"域
    assert infer_intent_from_tools(["get_zone_depth"]) == "popularity"
    assert infer_intent_from_tools(["get_movement_paths"]) == "popularity"
    assert infer_intent_from_tools(["get_anomaly_alerts"]) == "anomaly"
    assert infer_intent_from_tools(["get_emotion_stats"]) == "emotion"


def test_zero_or_irrelevant_tools_is_general():
    """什么都没调、或只调了记忆/销量/地图类工具 → `general`（不要误标成业务域）。"""
    assert infer_intent_from_tools([]) == "general"
    assert infer_intent_from_tools(["search_chat_history"]) == "general"
    assert infer_intent_from_tools(["get_ops_archive"]) == "general"
    assert infer_intent_from_tools(["get_sales_comparison"]) == "general"


def test_duplicate_and_prefixed_names_are_handled():
    """重复调用、带 `get_` 前缀/别名的写法都要认（CI 日志里两者都有）。"""
    assert infer_intent_from_tools(["get_anomaly_alerts"] * 6) == "anomaly"
    assert infer_intent_from_tools(["hourly_traffic", "anomaly_alerts"]) == "both"


def test_case_expectations_are_compatible_with_the_mapping():
    """静态一致性：凡是用例写了 `expect_tools` 的，映射推断出的意图必须在 `expect_intent` 允许集合里。

    这条守的是"改了域映射之后，别的用例被顺手改坏"——它不会覆盖没写 expect_tools 的用例
    （例如 mem_04），那些靠上面的顺序无关用例兜。
    """
    cases_file = PROJECT_ROOT / "evals" / "cases.json"
    cases = json.loads(io.open(cases_file, encoding="utf-8").read())["cases"]
    bad = []
    for c in cases:
        tools = c.get("expect_tools") or []
        expect = c.get("expect_intent")
        if not tools or not expect:
            continue
        allowed = expect if isinstance(expect, list) else [expect]
        got = infer_intent_from_tools(tools)
        if got not in allowed:
            bad.append(f"{c['id']}: 工具 {tools} → 推断 {got}，期望允许 {allowed}")
    assert not bad, "域映射与用例期望不一致：\n  " + "\n  ".join(bad)
