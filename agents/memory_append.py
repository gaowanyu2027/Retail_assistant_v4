"""模板轮记忆补写（多轮上下文，零 LLM 成本）。

**为什么单独一个模块**：这段逻辑要能被**离线单测**覆盖（CI 的"最小依赖"job 里
没有 langchain / torch）。如果放在 `agents/master_agent.py` 里，测试就得
`import agents.master_agent` —— 那是一条重依赖链（langchain + langgraph +
langchain_openai），CI 里直接导入失败。所以把这块纯逻辑抠出来，
只依赖"传进来的图对象"和"消息类"两样东西。

**背景（实测）**：多轮上下文的唯一来源是 LangGraph 检查点按 thread_id 恢复的消息，
而意图路由命中的模板轮（零 LLM）走 `route_intent → skill` 直接返回、**不经过图** →
这一轮对下一轮的 LLM 不存在：

    第1轮「1号货架今天客流怎么样？」(模板直答，不写检查点)
    第2轮「我刚才问的是什么？」   → "本次对话里你还没有问过其他内容，这是第一条消息" ❌

修复就是这里：模板轮结束后把这一问一答追加进同一 thread。
"""
from __future__ import annotations

import threading
from typing import Any

# 同一会话的"读状态 → 删旧 → 追加"必须串行：并发写会拿到同一批待删 id，
# 先写的那次删掉后，后写的那次会报 "Attempting to delete a message with an ID
# that doesn't exist"（实测 c100 下 1000 次写里有 156 次因此丢失）。
# 按 thread_id 分锁，不同会话互不阻塞；表有界，超限就整体清空重建。
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()
_THREAD_LOCKS_MAX = 512


def _lock_for(thread_id: str) -> threading.Lock:
    with _thread_locks_guard:
        lock = _thread_locks.get(thread_id)
        if lock is None:
            if len(_thread_locks) >= _THREAD_LOCKS_MAX:
                _thread_locks.clear()      # 极端情况下重建；已持有的锁对象不受影响
            lock = _thread_locks[thread_id] = threading.Lock()
        return lock


def _default_message_classes():
    """LangChain 消息类（延迟导入：只有真正要写检查点时才需要 langchain_core）。"""
    from langchain_core.messages import AIMessage, HumanMessage

    return HumanMessage, AIMessage


def _default_remove_cls():
    """`RemoveMessage`（延迟导入，理由同 `_default_message_classes`）。"""
    from langchain_core.messages import RemoveMessage

    return RemoveMessage


def _removals_for_cap(graph: Any, config: dict, *, max_turns: int,
                      remove_cls: type) -> list:
    """读出线程已有消息，返回需要删除的 `RemoveMessage` 列表。

    **为什么裁**：检查点每轮整行重写、行内含整段会话，成本随轮数线性增长
    （实测 22.7KB 行 11.6ms/次 → 114KB 行 32.4ms/次），而全部写入被
    MysqlCheckpointer 的进程级单锁 + 单连接串行化。只保留最近 N 轮后，
    行大小稳定在 ~N×1.3KB。

    **失败一律返回空列表**（宁可不裁，也不能把线程写坏）。
    """
    state = graph.get_state(config)
    values = getattr(state, "values", None) or {}
    messages = list(values.get("messages") or [])
    keep_existing = max(0, 2 * max_turns - 2)       # 本次还要追加一问一答
    overflow = len(messages) - keep_existing
    if overflow <= 0:
        return []
    removals = []
    for msg in messages[:overflow]:
        msg_id = getattr(msg, "id", None)
        if not msg_id:                              # 没有 id 无从删除
            return []
        removals.append(remove_cls(id=msg_id))
    return removals


def append_template_turn(
    graph: Any,
    session_id: str,
    query: str,
    answer: str,
    *,
    human_cls: type | None = None,
    ai_cls: type | None = None,
    remove_cls: type | None = None,
    enabled: bool | None = None,
    max_turns: int | None = None,
) -> bool:
    """把一轮"模板问答"追加进该会话的检查点线程。

    Args:
        graph: LangGraph 编译后的图（需要支持 `update_state(config, values)`；
            支持 `get_state(config)` 时才会按 `max_turns` 裁剪旧消息）
        session_id: 会话 ID（= thread_id）
        query / answer: 这一轮的问答文本
        human_cls / ai_cls / remove_cls: 消息类，默认取 langchain_core 的
            HumanMessage / AIMessage / RemoveMessage —— 留出注入点是为了让单测在
            **不装 langchain** 的环境里也能验证"写什么、写成什么结构"（CI 最小依赖 job）。
        enabled: 开关（默认读 `config.settings.AGENT_REMEMBER_TEMPLATE_TURNS`）；
            关掉即退回旧行为，省掉每次模板请求的一次 MySQL 检查点写入。
        max_turns: 保留的最近轮数（默认读 `AGENT_TEMPLATE_MEMORY_MAX_TURNS`，默认 10；
            0 = 不裁剪）。裁剪只在 `get_state` 可用时生效。

    Returns:
        True=写入成功；False=被开关关掉/空回答/写入失败（已吞掉异常）。
        **调用方不应因为记忆失败而失败**：模板路径的意义就是毫秒级可用。
    """
    if enabled is None:
        from config.settings import AGENT_REMEMBER_TEMPLATE_TURNS as enabled
    if not enabled or not answer:
        return False
    try:
        if human_cls is None or ai_cls is None:
            human_cls, ai_cls = _default_message_classes()
        mid = str(session_id or "default")
        # ⚠ thread_id 必须放在 configurable 这一层：checkpointer 直接取
        # config["configurable"]["thread_id"]，顶层写法会 KeyError（实测）
        config = {"configurable": {"thread_id": mid}}
        new_messages = [human_cls(content=query), ai_cls(content=answer)]

        if max_turns is None:
            from config.settings import AGENT_TEMPLATE_MEMORY_MAX_TURNS as max_turns

        with _lock_for(mid):
            removals = []
            if max_turns and max_turns > 0:
                try:
                    if remove_cls is None:
                        remove_cls = _default_remove_cls()
                    removals = _removals_for_cap(
                        graph, config, max_turns=max_turns, remove_cls=remove_cls
                    )
                except Exception:
                    pass                            # 读状态失败：只追加、不裁剪
            try:
                graph.update_state(config, {"messages": removals + new_messages})
            except Exception:
                if not removals:
                    raise
                # 兜底：删旧失败（例如别的写入者已删过）就退回只追加，
                # 宁可多留几轮，也不能把这一轮的记忆丢掉。
                graph.update_state(config, {"messages": new_messages})
        return True
    except Exception as e:                      # 失败只打印，绝不影响回答
        print(f"[TemplateMemory] 模板轮写入检查点失败(不影响回答): {e}")
        return False
