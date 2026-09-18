"""多轮上下文：模板轮必须进检查点线程（否则下一轮 LLM 看不到"上一轮问了什么"）。

背景（实测，见 改进记录 D18）：
  - 多轮上下文的唯一来源是 LangGraph 检查点按 thread_id 恢复的消息；
  - 但意图路由命中的模板轮（零 LLM，走 `route_intent → skill` 直答）**完全不经过图**，
    所以这一轮对下一轮的 LLM 不存在：

        第1轮「1号货架今天客流怎么样？」→ 模板直答
        第2轮「我刚才问的是什么？」   → "这是本次对话的第一条消息"   ❌

修复：`MasterAgent.quick_answer` 在返回模板回答前，把这一问一答
用 `update_state` 追加进同一 thread（零 LLM 成本）。

本测试只验证**这段逻辑本身**（用桩图，不连云、不调 LLM）：
  1. 开关打开 → 写一次，且 config/消息结构正确（Human 在前、AI 在后）；
  2. 开关关闭 → 完全不写（可退回旧行为，省一次 MySQL 写入）；
  3. 写入异常 → **不抛出**（模板路径的意义是毫秒级可用，记忆失败不能拖垮回答）；
  4. 空回答不写垃圾进线程。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.master_agent import MasterAgent  # noqa: E402


class _StubGraph:
    """记录 update_state 调用的桩图（不连任何数据库）。"""

    def __init__(self, boom: bool = False):
        self.calls: list[tuple] = []
        self.boom = boom

    def update_state(self, config, values):
        if self.boom:
            raise RuntimeError("checkpointer down")
        self.calls.append((config, values))


class _StubAgent:
    def __init__(self, boom: bool = False):
        self.agent = _StubGraph(boom=boom)


def _call(stub, q="1号货架今天客流怎么样？", a="当前最热的是1号货架…", sid="s1"):
    """绑定到 MasterAgent 的真实实现上调用（不需要构造 Agent 实例）。"""
    return MasterAgent._remember_template_turn(stub, q, a, sid)


def test_template_turn_is_written_to_thread():
    stub = _StubAgent()
    _call(stub)
    assert len(stub.agent.calls) == 1, stub.agent.calls
    config, values = stub.agent.calls[0]
    # thread_id 必须走 configurable（checkpointer 只认这一层；顶层会被 KeyError）
    assert config == {"configurable": {"thread_id": "s1"}}, config
    msgs = values["messages"]
    assert len(msgs) == 2 and msgs[0].content.startswith("1号货架"), msgs
    assert type(msgs[0]).__name__ == "HumanMessage" and type(msgs[1]).__name__ == "AIMessage", msgs


def test_flag_off_keeps_old_behaviour():
    """关掉开关 → 一次都不写（给高并发/低延迟场景留退路）。"""
    import config.settings as settings

    stub = _StubAgent()
    old = settings.AGENT_REMEMBER_TEMPLATE_TURNS
    settings.AGENT_REMEMBER_TEMPLATE_TURNS = False
    try:
        _call(stub)
    finally:
        settings.AGENT_REMEMBER_TEMPLATE_TURNS = old
    assert stub.agent.calls == [], stub.agent.calls


def test_write_failure_never_breaks_the_answer():
    """记忆写入失败必须被吞掉：模板轮不能因为检查点故障而报错。"""
    stub = _StubAgent(boom=True)
    _call(stub)      # 不抛异常即为通过


def test_empty_answer_is_not_written():
    stub = _StubAgent()
    _call(stub, a="")
    assert stub.agent.calls == [], stub.agent.calls
