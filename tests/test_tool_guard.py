"""工具调用护栏（`agents/tool_guard.py`）的离线单测（2026-09-23）。

背景：CI run#21 里 Agent 把同一对工具反复调了约 30 次（空数据时不停重试），
回答退化成"重复查询不会有新结果"，成本与延迟被放大十几倍。
护栏做两件事：相同(工具,参数)只执行一次 + 每轮总次数上限。
本文件**不引入 langchain**（CI 的 unit job 只装最小依赖），安装逻辑用假 factory 覆盖。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents import tool_guard  # noqa: E402


# ⚠ 注意：本项目用自研运行器（tests/run_tests.py），它**不调用** pytest 的 setup_function，
# 所以每个用例都自己显式 `tool_guard.reset()` —— 否则上一轮的护栏状态会泄漏到下一个用例。


def test_no_turn_registered_is_passthrough():
    tool_guard.reset()   # 显式复位：本项目运行器不调用 pytest 的 setup_function
    """没登记轮次（脚本/单测直连工具）→ 完全直通，行为不变。"""
    calls = []

    def ex():
        calls.append(1)
        return json.dumps({"ok": len(calls)})

    for _ in range(5):
        out = tool_guard.guard_call("t", {}, ex)
        assert json.loads(out)["ok"] == len(calls)
    assert len(calls) == 5, "未启用护栏时不该去重"


def test_max_calls_zero_means_unlimited():
    tool_guard.reset()   # 显式复位：本项目运行器不调用 pytest 的 setup_function
    tool_guard.set_turn(1, 0)
    n = 0

    def ex():
        nonlocal n
        n += 1
        return "{}"

    for _ in range(20):
        tool_guard.guard_call("t", {"q": "x"}, ex)
    assert n == 20, "0 = 不限"
    assert tool_guard.current_state().dedup_hits == 0


def test_identical_call_is_deduplicated():
    tool_guard.reset()   # 显式复位：本项目运行器不调用 pytest 的 setup_function
    """相同（工具, 参数）第二次不再执行，返回首次结果 + 说明。"""
    tool_guard.set_turn(1, 8)
    n = 0

    def ex():
        nonlocal n
        n += 1
        return json.dumps({"n": n})

    a = tool_guard.guard_call("get_hourly_traffic", {"query": ""}, ex)
    b = tool_guard.guard_call("get_hourly_traffic", {"query": ""}, ex)
    assert n == 1, "相同参数被重复执行了"
    assert json.loads(a)["n"] == 1
    assert json.loads(b)["n"] == 1 and "_note" in json.loads(b), "重复调用应带回说明"
    st = tool_guard.current_state()
    assert st.calls == 1 and st.dedup_hits == 1


def test_different_args_are_not_deduplicated():
    tool_guard.reset()   # 显式复位：本项目运行器不调用 pytest 的 setup_function
    """参数不同 = 不同调用（例如换了时段/区域），必须照常执行。"""
    tool_guard.set_turn(1, 8)
    n = 0

    def ex():
        nonlocal n
        n += 1
        return "{}"

    tool_guard.guard_call("get_zone_depth", {"zone": "1号"}, ex)
    tool_guard.guard_call("get_zone_depth", {"zone": "2号"}, ex)
    assert n == 2


def test_arg_order_does_not_matter():
    tool_guard.reset()   # 显式复位：本项目运行器不调用 pytest 的 setup_function
    """指纹按参数名排序 → 同一调用换参数顺序仍算重复。"""
    tool_guard.set_turn(1, 8)
    n = 0

    def ex():
        nonlocal n
        n += 1
        return "{}"

    tool_guard.guard_call("t", {"a": 1, "b": 2}, ex)
    tool_guard.guard_call("t", {"b": 2, "a": 1}, ex)
    assert n == 1, "参数顺序影响指纹 → 去重失效"


def test_total_cap_returns_hint_instead_of_executing():
    tool_guard.reset()   # 显式复位：本项目运行器不调用 pytest 的 setup_function
    """超过每轮上限 → 不再执行，返回提示（让模型基于已有信息作答）。"""
    tool_guard.set_turn(1, 3)
    seen = []

    def ex():
        seen.append(1)
        return json.dumps({"i": len(seen)})

    outs = [tool_guard.guard_call("t", {"i": i}, ex) for i in range(3)]
    capped = tool_guard.guard_call("t", {"i": 99}, ex)
    assert len(seen) == 3, f"上限是 3，实际执行了 {len(seen)} 次"
    assert "上限" in json.loads(capped)["_note"], capped
    assert tool_guard.current_state().capped is True
    assert all("_note" not in json.loads(o) for o in outs)


def test_new_turn_resets_budget_and_cache():
    tool_guard.reset()   # 显式复位：本项目运行器不调用 pytest 的 setup_function
    """新一轮必须重新计数（否则第二条提问会被上一轮的用量拖累）。"""
    tool_guard.set_turn(1, 1)
    n = 0

    def ex():
        nonlocal n
        n += 1
        return "{}"

    tool_guard.guard_call("t", {"q": "a"}, ex)
    tool_guard.guard_call("t", {"q": "b"}, ex)          # 本轮已到上限
    assert n == 1
    tool_guard.set_turn(2, 1)                            # 新的一轮
    tool_guard.guard_call("t", {"q": "a"}, ex)           # 同参数也应重新执行
    assert n == 2, "新一轮没有重置预算/缓存"


def test_install_guard_wraps_and_degrades_gracefully():
    tool_guard.reset()   # 显式复位：本项目运行器不调用 pytest 的 setup_function
    """安装逻辑：正常工具被包一层；没有 `func` 的工具（MCP/仅异步）原样保留。"""
    def real(query: str = "") -> str:
        return json.dumps({"ok": True})

    class FakeTool:
        def __init__(self, name, func=None, description="d", args_schema=None):
            self.name, self.func, self.description, self.args_schema = name, func, description, args_schema

    made = {}

    def fake_factory(**kw):
        made[kw["name"]] = kw
        return FakeTool(kw["name"], kw["func"], kw.get("description"), kw.get("args_schema"))

    asynconly = FakeTool("mcp_tool")            # 只有 coroutine、没有 func
    tools = [FakeTool("get_hourly_traffic", real), asynconly]
    out = tool_guard.install_guard(tools, factory=fake_factory)

    assert len(out) == 2
    assert "get_hourly_traffic" in made, "普通工具没有被包装"
    assert out[1] is asynconly, "无 func 的工具应原样保留"

    # 包好的工具确实受护栏管辖
    tool_guard.set_turn(1, 2)
    wrapped = made["get_hourly_traffic"]["func"]
    first, second = wrapped(query=""), wrapped(query="")
    assert json.loads(first)["ok"] is True
    assert "_note" in json.loads(second), "包装后的工具没有走护栏"


def test_factory_failure_keeps_original_tool():
    tool_guard.reset()   # 显式复位：本项目运行器不调用 pytest 的 setup_function
    """包装失败 → 保留原工具（护栏可以少，Agent 不能起不来）。"""
    def real():
        return "{}"

    class FakeTool:
        name, func, description, args_schema = "t", real, "d", None

    def boom(**_kw):
        raise RuntimeError("factory 炸了")

    t = FakeTool()
    out = tool_guard.install_guard([t], factory=boom)
    assert out == [t], "包装失败时应原样保留"


def test_real_langchain_tool_roundtrip_preserves_schema():
    """用**真实** LangChain 工具验证：包装后 name / description / 参数 schema 不变，且护栏生效。

    为什么单列一条：包装工具是"改运行时行为"的操作，最容易的翻车方式是
    `StructuredTool.from_function` 从 `*args, **kwargs` 推断出**空参数 schema** ——
    那样 LLM 会看到一个"不接受任何参数的工具"，比不装护栏坏得多。
    本地装了 langchain 才跑（CI 的 unit job 只装最小依赖，这里会打印 SKIP 并放过）。
    """
    tool_guard.reset()
    try:
        from langchain_core.tools import tool as lc_tool
    except Exception as e:                                    # pragma: no cover
        print(f"       [SKIP] 未安装 langchain（最小依赖环境）：{e}")
        return

    @lc_tool
    def get_hourly_traffic(query: str = "") -> str:
        """时段客流分析（测试替身）"""
        return json.dumps({"rows": 0})

    orig = get_hourly_traffic
    wrapped = tool_guard.install_guard([orig])[0]

    assert wrapped.name == orig.name, f"名称变了：{wrapped.name} != {orig.name}"
    assert (wrapped.description or "").startswith("时段客流分析"), wrapped.description
    props = (wrapped.args_schema.model_json_schema().get("properties") or {}) if wrapped.args_schema else {}
    assert "query" in props, f"参数 schema 丢了（会变成无参数工具）: {props}"

    tool_guard.set_turn(1, 2)
    first = wrapped.invoke({"query": ""})
    second = wrapped.invoke({"query": ""})
    assert json.loads(first)["rows"] == 0
    assert "_note" in json.loads(second), "包装后的真实工具没有走护栏"
