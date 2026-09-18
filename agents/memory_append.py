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

from typing import Any


def _default_message_classes():
    """LangChain 消息类（延迟导入：只有真正要写检查点时才需要 langchain_core）。"""
    from langchain_core.messages import AIMessage, HumanMessage

    return HumanMessage, AIMessage


def append_template_turn(
    graph: Any,
    session_id: str,
    query: str,
    answer: str,
    *,
    human_cls: type | None = None,
    ai_cls: type | None = None,
    enabled: bool | None = None,
) -> bool:
    """把一轮"模板问答"追加进该会话的检查点线程。

    Args:
        graph: LangGraph 编译后的图（需要支持 `update_state(config, values)`）
        session_id: 会话 ID（= thread_id）
        query / answer: 这一轮的问答文本
        human_cls / ai_cls: 消息类，默认取 langchain_core 的 HumanMessage / AIMessage
            —— 留出注入点是为了让单测在**不装 langchain** 的环境里也能验证
            "写什么、写成什么结构"（CI 最小依赖 job）。
        enabled: 开关（默认读 `config.settings.AGENT_REMEMBER_TEMPLATE_TURNS`）；
            关掉即退回旧行为，省掉每次模板请求的一次 MySQL 检查点写入。

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
        graph.update_state(
            {"configurable": {"thread_id": mid}},
            {"messages": [human_cls(content=query), ai_cls(content=answer)]},
        )
        return True
    except Exception as e:                      # 失败只打印，绝不影响回答
        print(f"[TemplateMemory] 模板轮写入检查点失败(不影响回答): {e}")
        return False
