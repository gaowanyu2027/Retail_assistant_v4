"""多轮上下文：模板轮必须进检查点线程（否则下一轮 LLM 看不到"上一轮问了什么"）。

背景（实测，见 改进记录 A21）：
  - 多轮上下文的唯一来源是 LangGraph 检查点按 thread_id 恢复的消息；
  - 但意图路由命中的模板轮（零 LLM，走 `route_intent → skill` 直答）**完全不经过图**，
    所以这一轮对下一轮的 LLM 不存在：

        第1轮「1号货架今天客流怎么样？」→ 模板直答
        第2轮「我刚才问的是什么？」   → "这是本次对话的第一条消息"   ❌

修复：模板轮结束后把这一问一答用 `update_state` 追加进同一 thread（零 LLM 成本），
实现见 `agents/memory_append.py`。

本测试只验证**这段逻辑本身**，刻意做到**零重依赖**（桩图 + 桩消息类，
不连云、不调 LLM、不 import master_agent）——因为 CI 的"最小依赖"单测 job
里只有 fastapi/httpx/qdrant-client。第一版测试 import 了 `agents.master_agent`，
CI 首次真跑就挂了 10 个文件（见 改进记录"CI 首跑"一节）。
覆盖：
  1. 写入内容与结构正确（config 走 configurable / Human 在前 AI 在后）；
  2. 开关关闭 → 完全不写（可退回旧行为，省一次 MySQL 写入）；
  3. 写入异常 → **不抛出**（模板路径的意义是毫秒级可用）；
  4. 空回答不写垃圾进线程；
  5. 超过 `max_turns` 的旧消息被裁剪（检查点每轮整行重写，行会随会话线性变大）。
"""
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.memory_append import append_template_turn  # noqa: E402


class _Msg:
    """桩消息类（替代 langchain_core 的 HumanMessage / AIMessage）。"""

    def __init__(self, content="", id=None):
        self.content = content
        self.id = id

    def __repr__(self):
        return f"{type(self).__name__}({self.content!r})"


class _Human(_Msg):
    pass


class _AI(_Msg):
    pass


class _Remove:
    """桩 RemoveMessage。"""

    def __init__(self, id):
        self.id = id

    def __repr__(self):
        return f"RemoveMessage({self.id!r})"


class _StubGraph:
    """记录 update_state 调用的桩图（不连任何数据库）。"""

    def __init__(self, boom: bool = False, history=None, get_state_boom: bool = False):
        self.calls: list[tuple] = []
        self.boom = boom
        self.history = list(history or [])
        self.get_state_boom = get_state_boom

    def get_state(self, config):
        if self.get_state_boom:
            raise RuntimeError("state unavailable")
        return types.SimpleNamespace(values={"messages": list(self.history)})

    def update_state(self, config, values):
        if self.boom:
            raise RuntimeError("checkpointer down")
        self.calls.append((config, values))


def _call(graph, q="1号货架今天客流怎么样？", a="当前最热的是1号货架…", sid="s1"):
    return append_template_turn(graph, sid, q, a, human_cls=_Human, ai_cls=_AI)


def test_template_turn_is_written_to_thread():
    g = _StubGraph()
    assert _call(g) is True
    assert len(g.calls) == 1, g.calls
    config, values = g.calls[0]
    # thread_id 必须走 configurable（checkpointer 只认这一层；顶层会 KeyError）
    assert config == {"configurable": {"thread_id": "s1"}}, config
    msgs = values["messages"]
    assert len(msgs) == 2, msgs
    assert isinstance(msgs[0], _Human) and msgs[0].content.startswith("1号货架"), msgs
    assert isinstance(msgs[1], _AI) and msgs[1].content, msgs


def test_missing_session_id_falls_back_to_default():
    """session_id 为空也不能把 thread 写成 None（会污染检查点表）。"""
    g = _StubGraph()
    append_template_turn(g, "", "q", "a", human_cls=_Human, ai_cls=_AI)
    assert g.calls[0][0] == {"configurable": {"thread_id": "default"}}, g.calls[0][0]


def test_flag_off_keeps_old_behaviour():
    """关掉开关 → 一次都不写（给高并发/低延迟场景留退路）。

    两条路径都覆盖：显式入参 `enabled=False`，以及 `config.settings` 全局开关
    （生产走的是后者，靠 monkeypatch 验证 —— 不 import master_agent，保持离线可跑）。
    """
    import config.settings as settings

    g = _StubGraph()
    assert append_template_turn(g, "s1", "q", "a", human_cls=_Human, ai_cls=_AI,
                                enabled=False) is False
    assert g.calls == [], g.calls

    old = settings.AGENT_REMEMBER_TEMPLATE_TURNS
    settings.AGENT_REMEMBER_TEMPLATE_TURNS = False
    try:
        g2 = _StubGraph()
        assert append_template_turn(g2, "s1", "q", "a", human_cls=_Human, ai_cls=_AI) is False
        assert g2.calls == [], g2.calls
    finally:
        settings.AGENT_REMEMBER_TEMPLATE_TURNS = old


def test_env_switch_controls_setting():
    """环境变量 `AGENT_REMEMBER_TEMPLATE_TURNS` 能真正控制全局开关。

    配置项此前是硬编码 True，文档却把它描述成开关；压测需要按环境关闭
    （省掉每次模板请求的一次 MySQL 检查点写入）。这里通过重载 settings
    验证 0/1 两种取值，最后恢复原值，避免污染后续用例。
    """
    import importlib
    import os

    import config.settings as settings

    key = "AGENT_REMEMBER_TEMPLATE_TURNS"
    old_env = os.environ.get(key)
    old_val = settings.AGENT_REMEMBER_TEMPLATE_TURNS
    try:
        os.environ[key] = "0"
        importlib.reload(settings)
        assert settings.AGENT_REMEMBER_TEMPLATE_TURNS is False
        os.environ[key] = "1"
        importlib.reload(settings)
        assert settings.AGENT_REMEMBER_TEMPLATE_TURNS is True
    finally:
        if old_env is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old_env
        importlib.reload(settings)
        assert settings.AGENT_REMEMBER_TEMPLATE_TURNS == old_val


def test_write_failure_never_breaks_the_answer():
    """记忆写入失败必须被吞掉：模板轮不能因为检查点故障而报错。"""
    g = _StubGraph(boom=True)
    assert _call(g) is False          # 不抛异常即为通过


def test_empty_answer_is_not_written():
    g = _StubGraph()
    assert _call(g, a="") is False
    assert g.calls == [], g.calls


def test_old_turns_are_trimmed_to_cap():
    """max_turns=3：12 条历史 → 只留最近 2 轮（4 条），再加本次一问一答。"""
    history = []
    for i in range(6):
        history.append(_Human("问题%d" % i, id="h%d" % i))
        history.append(_AI("回答%d" % i, id="a%d" % i))
    g = _StubGraph(history=history)
    ok = append_template_turn(g, "s1", "新问题", "新回答", human_cls=_Human, ai_cls=_AI,
                              remove_cls=_Remove, max_turns=3)
    assert ok is True
    _, values = g.calls[0]
    msgs = values["messages"]
    # 前 4 轮（8 条）删掉，留最近 2 轮 + 本次一问一答
    assert [m.id for m in msgs[:8]] == ["h0", "a0", "h1", "a1", "h2", "a2", "h3", "a3"], msgs
    assert all(isinstance(m, _Remove) for m in msgs[:8]), msgs
    assert isinstance(msgs[-2], _Human) and isinstance(msgs[-1], _AI), msgs


def test_no_trim_when_under_cap():
    history = [_Human("q", id="h0"), _AI("a", id="a0")]
    g = _StubGraph(history=history)
    append_template_turn(g, "s1", "q", "a", human_cls=_Human, ai_cls=_AI,
                         remove_cls=_Remove, max_turns=3)
    msgs = g.calls[0][1]["messages"]
    assert len(msgs) == 2, msgs
    assert all(isinstance(m, _Msg) for m in msgs), msgs


def test_get_state_failure_falls_back_to_append_only():
    """读状态失败 → 只追加、不裁剪，写入本身照常成功。"""
    g = _StubGraph(get_state_boom=True)
    assert _call(g) is True
    assert len(g.calls[0][1]["messages"]) == 2, g.calls


def test_max_turns_zero_disables_trimming():
    history = [_Human("q%d" % i, id="h%d" % i) for i in range(10)]
    g = _StubGraph(history=history)
    append_template_turn(g, "s1", "q", "a", human_cls=_Human, ai_cls=_AI,
                         remove_cls=_Remove, max_turns=0)
    assert len(g.calls[0][1]["messages"]) == 2, g.calls


def test_messages_without_ids_skip_trimming():
    """历史消息没有 id 时无从删除 → 放弃裁剪，绝不写坏线程。"""
    history = [_Human("q%d" % i) for i in range(10)]
    g = _StubGraph(history=history)
    append_template_turn(g, "s1", "q", "a", human_cls=_Human, ai_cls=_AI,
                         remove_cls=_Remove, max_turns=3)
    assert len(g.calls[0][1]["messages"]) == 2, g.calls


class _FlakyGraph(_StubGraph):
    """第一次带删除的写入失败（模拟并发删同一个 id），之后正常。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.delete_seen = False

    def update_state(self, config, values):
        has_remove = any(isinstance(v, _Remove) for v in values["messages"])
        if has_remove and not self.delete_seen:
            self.delete_seen = True
            raise RuntimeError(
                "Attempting to delete a message with an ID that doesn't exist ('x')"
            )
        super().update_state(config, values)


def test_delete_failure_falls_back_to_append_only():
    """删旧失败不能丢这一轮：退回只追加，写入仍算成功。"""
    history = []
    for i in range(6):
        history.append(_Human("问题%d" % i, id="h%d" % i))
        history.append(_AI("回答%d" % i, id="a%d" % i))
    g = _FlakyGraph(history=history)
    ok = append_template_turn(g, "s1", "新问题", "新回答", human_cls=_Human, ai_cls=_AI,
                              remove_cls=_Remove, max_turns=3)
    assert ok is True
    assert len(g.calls) == 1, g.calls
    assert len(g.calls[0][1]["messages"]) == 2, g.calls
