"""一轮问答内的**工具调用护栏**：① 相同（工具, 参数）只执行一次；② 每轮工具调用总数上限。

## 为什么需要（CI run#21 实测，不是理论问题）

空数据（CI 库为空 / 线上采集未启动）时，Agent 把 `get_hourly_traffic` 与 `get_anomaly_alerts`
**反复交替调用了约 30 次**，回答退化成"实时结果与上次相同…重复查询不会有新结果"。后果：

1. **成本与延迟放大**：每次工具调用都是一轮 LLM 往返（30 次 ≈ 十几倍开销）；
2. **答案质量下降**：模型在"再查一次"上耗尽预算，而不是基于已有信息作答；
3. **评测被污染**：意图由工具调用反推 → 被标成 `anomaly`，与用例期望不符。

## 机制

- `set_turn(turn_id, max_calls)`：每次调用 Agent 前登记一次"轮次"，护栏只在这一轮内生效；
- 状态放在 `ContextVar` 里 —— 与 `auth_context.py` 传账号进工具用的是同一套机制（该项目已验证可靠）；
- 未调用 `set_turn` 时（单测、脚本直连工具）**完全直通**，不改变任何行为；
- `max_calls <= 0` 表示**不限**（可用环境变量关掉）。

## 替代结果长什么样

工具统一返回 JSON 字符串，所以：
- **重复调用** → 原结果 + `_note` 字段（说明"本轮已查过相同参数，无新数据"）；
- **超上限** → 一个只含提示的 JSON（让模型基于已有信息作答，而不是继续调工具）。
"""
import json
from contextvars import ContextVar
from typing import Any, Callable

# 未设置轮次时为 None —— 此时护栏直通（不影响单测/脚本）
_TURN_STATE: ContextVar[Any] = ContextVar("tool_turn_state", default=None)

REPEAT_NOTE = "本轮已用相同参数查过一次，结果同上（无新数据）。请勿重复查询，基于已有信息作答。"
CAPPED_NOTE = "本轮工具调用次数已达上限。请立即基于已获得的数据作答，不要再调用工具。"


class ToolTurnState:
    """一轮问答内的护栏状态（不要跨轮复用）。"""

    __slots__ = ("turn_id", "max_calls", "calls", "seen", "dedup_hits", "capped")

    def __init__(self, turn_id: int, max_calls: int):
        self.turn_id = turn_id
        self.max_calls = max_calls
        self.calls = 0                 # 实际执行过的工具调用数
        self.seen: dict[str, str] = {}  # 归一化 key → 首次结果
        self.dedup_hits = 0            # 被去重挡下的次数
        self.capped = False            # 是否触发过上限

    def summary(self) -> str:
        return (f"工具调用 {self.calls} 次"
                + (f"（去重 {self.dedup_hits} 次）" if self.dedup_hits else "")
                + ("（已达上限）" if self.capped else ""))


def set_turn(turn_id: int, max_calls: int) -> None:
    """登记新一轮（每次调用 Agent 前调用一次）。`max_calls <= 0` = 不限。"""
    _TURN_STATE.set(ToolTurnState(turn_id, max_calls))


def current_state() -> ToolTurnState | None:
    return _TURN_STATE.get()


def reset() -> None:
    """清掉当前轮次（测试用）。"""
    _TURN_STATE.set(None)


def _key(name: str, args: dict) -> str:
    """归一化调用指纹：**参数顺序无关**、值类型稳定（同一次提问里 `query` 常为空串）。"""
    try:
        norm = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        norm = str(sorted((str(k), str(v)) for k, v in (args or {}).items()))
    return f"{name}|{norm}"


def _with_note(result: str, note: str) -> str:
    """给 JSON 结果加一个 `_note` 字段；非 JSON 就退化成追加一行说明。"""
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            data.setdefault("_note", note)
            return json.dumps(data, ensure_ascii=False, indent=2, default=str)
        if isinstance(data, list):
            return json.dumps({"_note": note, "data": data}, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass
    return result if note in result else f"{result}\n（{note}）"


def guard_call(name: str, args: dict, executor: Callable[[], str]) -> str:
    """护栏核心：决定"执行 / 返回缓存 / 拒绝"，是纯逻辑（便于离线单测）。

    - 未登记轮次 → 直通执行（不改变既有行为）；
    - 相同指纹已存在 → 返回首次结果 + 说明（不再真的执行）；
    - 本轮已达上限 → 返回一段提示（不再真的执行）。
    """
    state = _TURN_STATE.get()
    if state is None or state.max_calls <= 0:
        return executor()

    key = _key(name, args)
    if key in state.seen:
        state.dedup_hits += 1
        return _with_note(state.seen[key], REPEAT_NOTE)

    if state.calls >= state.max_calls:
        state.capped = True
        return json.dumps(
            {"_note": CAPPED_NOTE, "tool": name, "executed_calls": state.calls},
            ensure_ascii=False, indent=2)

    state.calls += 1
    out = executor()
    state.seen[key] = out
    return out


def _default_factory(**kwargs):
    """默认用 LangChain 的 `StructuredTool.from_function` 重建工具（延迟导入，单测可注入替身）。"""
    from langchain_core.tools import StructuredTool
    return StructuredTool.from_function(**kwargs)


def install_guard(tools: list, factory: Callable[..., Any] | None = None) -> list:
    """给工具列表逐个套上护栏。

    **失败要降级而不是报错**：任何一个工具包装失败，就原样保留它并打印一行警告 ——
    宁可少一个护栏，也不能让整个 Agent 起不来。（仅异步/MCP 工具没有 `func`，会被跳过。）
    """
    factory = factory or _default_factory
    out = []
    for t in tools or []:
        name = getattr(t, "name", None) or getattr(t, "__name__", "?")
        func = getattr(t, "func", None)
        if func is None:
            out.append(t)
            continue
        try:
            def wrapped(*a, __f=func, __n=name, **kw):
                args = dict(kw)
                if a:
                    args["_positional"] = [str(x) for x in a]
                return guard_call(__n, args, lambda: __f(*a, **kw))

            wrapped.__name__ = getattr(func, "__name__", "guarded_tool")
            out.append(factory(func=wrapped, name=name,
                               description=getattr(t, "description", "") or "",
                               args_schema=getattr(t, "args_schema", None)))
        except Exception as e:      # noqa: BLE001 —— 特意兜底：护栏不能影响可用性
            print(f"[ToolGuard] {name} 未套用护栏（保持原样）: {type(e).__name__}: {e}")
            out.append(t)
    return out
