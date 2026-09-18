"""一次性探针：多轮记忆到底有没有生效（checkpointer 是否把第 1 轮带进第 2 轮）。

背景：评测 `mem_ctx_02`（第 1 轮问"1号货架客流"，第 2 轮问"我刚才问的是什么？"）
第 2 轮回答"这是第一条消息" —— 怀疑 `agent.invoke(config={"thread_id": sid})`
（**少了 configurable 这一层**）导致 checkpointer 取不到该会话的历史。

这个探针不改任何代码，只做三件事：
  1. 用 `{"thread_id": sid}`（现状写法）跑两轮，打印第 2 轮回答 + 检查点里的消息条数；
  2. 用 `{"configurable": {"thread_id": sid}}`（LangGraph 标准写法）跑同样两轮做对照；
  3. 直接问 checkpointer：这两个 config 能不能读到同一个 thread 的状态。

用法：
    python tools/probe_multiturn_memory.py            # 只跑 1+2（各 2 次 LLM 调用）
    python tools/probe_multiturn_memory.py --no-llm   # 只做第 3 步（零 LLM 调用）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _msgs(state) -> int:
    """从 graph state（dict 或 StateSnapshot）里取消息条数。"""
    try:
        values = getattr(state, "values", None)
        if values is None:
            values = (state or {}).get("values", {})
        return len((values or {}).get("messages", []))
    except Exception:
        return -1


def probe_config_only(agent) -> None:
    """零 LLM：只用 checkpointer 的 get_tuple 验证两种 config 写法是否等价。"""
    from langchain_core.messages import HumanMessage

    sid = "probe_mt_cfg"
    # 先用图跑一轮？不——直接写一条检查点需要走图，这里改为读：若读不到就说明写法不被识别
    for label, cfg in (
        ("{'thread_id': sid}", {"thread_id": sid}),
        ("{'configurable': {'thread_id': sid}}", {"configurable": {"thread_id": sid}}),
    ):
        try:
            t = agent.memory.get_tuple(dict(cfg))
            print(f"[checkpointer] config={label} → {'读到检查点' if t else '无检查点(None)'}")
        except Exception as e:
            print(f"[checkpointer] config={label} → 异常 {type(e).__name__}: {e}")

    # 图侧：get_state 用的是标准 config；对比现状写法
    for label, cfg in (
        ("{'thread_id': sid}", {"thread_id": sid}),
        ("{'configurable': {'thread_id': sid}}", {"configurable": {"thread_id": sid}}),
    ):
        try:
            st = agent.agent.get_state(dict(cfg))
            print(f"[graph.get_state] config={label} → 消息 {_msgs(st)} 条")
        except Exception as e:
            print(f"[graph.get_state] config={label} → 异常 {type(e).__name__}: {e}")


def probe_two_turns(agent, sid: str, cfg_style: str) -> None:
    """真跑两轮，看第 2 轮能否引用第 1 轮。"""
    from langchain_core.messages import HumanMessage

    def cfg():
        if cfg_style == "flat":
            return {"thread_id": sid}
        return {"configurable": {"thread_id": sid}}

    q1 = "1号货架今天客流怎么样？"
    q2 = "我刚才问的是什么？"
    print(f"\n===== {sid}（config 写法: {cfg_style}）=====")
    for i, q in enumerate((q1, q2), 1):
        try:
            r = agent.agent.invoke({"messages": [HumanMessage(content=q)]}, config=cfg())
            ans = r["messages"][-1].content
            print(f"[第{i}轮] 问: {q}")
            print(f"       答: {str(ans)[:160]}")
            print(f"       state 消息数: {len(r.get('messages', []))}")
        except Exception as e:
            print(f"[第{i}轮] 调用异常 {type(e).__name__}: {e}")
    try:
        st = agent.agent.get_state({"configurable": {"thread_id": sid}})
        print(f"[标准 config 读该会话] 消息 {_msgs(st)} 条")
    except Exception as e:
        print(f"[标准 config 读该会话] 异常 {type(e).__name__}: {e}")


def probe_update_state(agent) -> None:
    """零 LLM：验证"把模板轮的问答手工写进检查点"这条路走不走得通。

    这是修复"模板轮不进检查点 → 下一轮丢上下文"的候选手段。
    要验证的点：空 thread（还没有任何检查点）上 update_state 会不会报错、
    以及写进去之后 `invoke` 能不能看到这两条消息。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    sid = "probe_mt_upd"
    cfg = {"configurable": {"thread_id": sid}}
    try:
        out = agent.agent.update_state(
            cfg, {"messages": [HumanMessage(content="模板轮问题"), AIMessage(content="模板轮回答")]}
        )
        print(f"[update_state 空 thread] OK，写入后状态消息 {_msgs(out)} 条")
    except Exception as e:
        print(f"[update_state 空 thread] 失败 {type(e).__name__}: {e}")
    try:
        st = agent.agent.get_state(cfg)
        print(f"[update_state 后读回] 消息 {_msgs(st)} 条")
    except Exception as e:
        print(f"[update_state 后读回] 异常 {type(e).__name__}: {e}")


def cleanup() -> None:
    """删掉本探针写进去的 `probe_*` 会话（检查点/问答历史/工具日志）。

    探针会真的调用 Agent（会写检查点），跑完不留垃圾，否则库里会攒一堆调试会话。
    """
    try:
        import mysql_db

        conn = mysql_db.get_connection()
        total = 0
        with conn.cursor() as cur:
            for tbl, col in (
                ("agent_checkpoints", "thread_id"),
                ("agent_checkpoint_writes", "thread_id"),
                ("agent_long_term_memory", "session_id"),
                ("query_history", "session_id"),
                ("chat_session", "session_id"),
                ("agent_tool_log", "session_id"),
            ):
                try:
                    cur.execute(f"DELETE FROM {tbl} WHERE {col} LIKE %s", ("probe_%",))
                    total += cur.rowcount
                except Exception:
                    pass
        conn.close()
        print(f"[cleanup] 探针会话已清理 {total} 行")
    except Exception as e:
        print(f"[cleanup] 清理失败(可忽略): {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="不调用 LLM（只看 checkpointer 行为）")
    args = ap.parse_args()

    from api.dependencies import (
        get_anomaly_skill, get_emotion_skill, get_popularity_skill,
    )
    from agents.master_agent import MasterAgent

    agent = MasterAgent(
        popularity_skill=get_popularity_skill(),
        anomaly_skill=get_anomaly_skill(),
        emotion_skill=get_emotion_skill(),
    )

    probe_config_only(agent)
    probe_update_state(agent)
    if args.no_llm:
        cleanup()
        return

    probe_two_turns(agent, "probe_mt_flat", "flat")          # 现状写法
    probe_two_turns(agent, "probe_mt_nested", "nested")      # 标准写法
    cleanup()


if __name__ == "__main__":
    main()
