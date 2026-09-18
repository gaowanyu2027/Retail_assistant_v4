# -*- coding: utf-8 -*-
"""
评测集跑分 — 量化 Agent 的路由准确率 / 工具调用正确率 / 回答要点命中

用法:
    python evals/run_evals.py            # 全量跑
    python evals/run_evals.py --case pop_01   # 跑单条（调试）

报告输出: evals/reports/run_<时间戳>.md（控制台同步打印）

评分规则:
- router 模式（模板路径）: 实际走了 quick_answer 且意图匹配 且 回答含全部关键词
- agent  模式（LLM 路径）: 实际调用的工具 ⊇ 预期工具 且 回答含全部关键词
- expect_tools=[] / expect_keywords=[] 表示该维度不考核
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# 兼容 Windows：进程环境可能未继承最新的用户级环境变量（Langfuse key 常配在
# 系统用户变量里），启动时从注册表补齐，保证评测用例的 trace 自动上报 Langfuse
if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
    try:
        import winreg
        _env_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
        for _name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            try:
                _val, _ = winreg.QueryValueEx(_env_key, _name)
                os.environ.setdefault(_name, str(_val))
            except FileNotFoundError:
                pass
    except Exception:
        pass

EVALS_DIR = Path(__file__).resolve().parent
REPORTS_DIR = EVALS_DIR / "reports"


def load_cases(path: str | None = None) -> list[dict]:
    """默认读 evals/cases.json；path 可以是绝对路径或相对仓库根/evals 目录的路径。"""
    if path:
        p = Path(path)
        if not p.is_absolute():
            cand = [EVALS_DIR / p, EVALS_DIR.parent / p]
            p = next((c for c in cand if c.exists()), cand[0])
    else:
        p = EVALS_DIR / "cases.json"
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    cases = data["cases"] if isinstance(data, dict) else data
    print(f"[cases] {p} → {len(cases)} 条")
    return cases


def get_agent():
    """按生产装配构造 Agent（与 api/dependencies 一致：热度+异常+表情三技能）。"""
    from api.dependencies import (
        get_anomaly_skill, get_popularity_skill, get_emotion_skill,
    )
    from agents.master_agent import MasterAgent
    return MasterAgent(
        popularity_skill=get_popularity_skill(),
        anomaly_skill=get_anomaly_skill(),
        emotion_skill=get_emotion_skill(),
    )


def query_tool_logs(session_id: str) -> list[str]:
    """从 agent_tool_log 查该会话实际调用的工具名（模板路径无日志，返回空）。"""
    import mysql_db
    conn = mysql_db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tool_name FROM agent_tool_log WHERE session_id=%s",
                (session_id,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _score(case: dict, actual_intent: str, tool_names: list[str], answer: str) -> list[str]:
    """单轮打分：意图（允许列表浮动）/ 工具 / 不应调工具 / 回答要点。"""
    failures: list[str] = []
    expect_intent = case.get("expect_intent")
    if expect_intent:
        # 支持列表：LLM 路径意图由工具调用反推，受随机性影响，允许合理浮动
        allowed = expect_intent if isinstance(expect_intent, list) else [expect_intent]
        if actual_intent not in allowed:
            failures.append(f"意图不符(期望 {expect_intent}, 实际 {actual_intent})")

    expect_tools = case.get("expect_tools") or []
    if expect_tools:
        missing = [t for t in expect_tools
                   if not any(t in n for n in tool_names)]
        if missing:
            failures.append(f"工具缺失({','.join(missing)}, 实际 {tool_names or '无'})")

    # 不应调用工具（防闲聊/无意义输入滥调工具）
    if case.get("expect_no_tools") and tool_names:
        failures.append(f"不应调用工具(实际调了 {tool_names})")

    expect_kws = case.get("expect_keywords") or []
    if expect_kws:
        missing_kw = [k for k in expect_kws if k not in answer]
        if missing_kw:
            failures.append(f"回答缺要点({','.join(missing_kw)})")

    # 任一命中即可（如拒绝措辞 "不能/无法/无权限" 出现其一即达标）
    expect_kws_any = case.get("expect_keywords_any") or []
    if expect_kws_any and not any(k in answer for k in expect_kws_any):
        failures.append(f"回答缺任一要点({','.join(expect_kws_any)})")

    # 禁止出现（如 prompt 注入场景不得泄露系统提示词/法律定性词）
    expect_no_kws = case.get("expect_no_keywords") or []
    leaked = [k for k in expect_no_kws if k in answer]
    if leaked:
        failures.append(f"回答包含禁止内容({','.join(leaked)})")
    return failures


def _simulate_source(case: dict) -> None:
    """设置评测进程里的"视频源"状态（决定数据可信度门禁走哪条分支）。

    为什么评测必须显式管这件事：可信度门禁按**进程内**的最后出帧时刻判定
    （`agents/data_quality.py` 的 `_last_frame`），而评测是**另一个进程**，
    没有任何视频线程 → 门禁恒判"视频源未启动" → 模板回答全部变成
    "【数据不可信】…本次不出热度结论"，于是断言关键词（到访/最热/表情）全都不出现。
    实测（未处理前）：83 条里 11 条失败，全部是这一条原因，**不是产品回归**。

    所以：
    - 默认：标记两路源"刚出过帧" → 等价于"摄像头在跑"，与用例的预期语境一致；
    - 用例声明 `"data_stale": true`：清空 → 专门验证门禁本身（见 gate_01/gate_02）。
    每轮都调用（`DATA_STALE_SECONDS=90`，长会话用例单轮耗时可能接近该阈值）。
    """
    from agents import data_quality

    if case.get("data_stale"):
        data_quality.reset()
        return
    data_quality.mark_frame(data_quality.SOURCE_RETAIL)
    data_quality.mark_frame(data_quality.SOURCE_EMOTION)


def _persist_turn(sid: str, q: str, answer: str, intent: str) -> None:
    """把本轮问答写进 query_history —— **对齐生产的写入路径**。

    生产里这件事由 API 层做（`api/routes/query.py::fire_persist` → `_persist_query_history`），
    每轮都会写，跨会话 `search_chat_history` 与向量召回都建立在这张表上。
    评测直接调 `agent.handle_query`，绕过了 API 层 → 不补这一步，
    "跨会话长期记忆"类断言（mem_ctx_03）**必然失败**，但那是评测没模拟生产，
    不是产品缺陷（两者必须区分，否则会去改没坏的东西）。

    失败只告警：单轮用例不需要这张表，跨会话用例靠 `preload_history` 也能自备数据。
    """
    try:
        import mysql_db
        mysql_db.save_query_history(sid, q, answer, intent or "general", 0.9)
    except Exception as e:
        print(f"[persist] 本轮写入 query_history 失败(跨会话断言可能失真): {e}")


def _run_turn(agent, q: str, sid: str, case: dict | None = None) -> dict:
    """执行一轮问答（模板或 Agent），返回该轮输出。"""
    from agents.intent_router import route_intent
    _simulate_source(case or {})
    started = time.time()
    intent = route_intent(q)
    quick = agent.quick_answer(intent, q, sid)
    tool_names: list[str] = []
    answer = ""
    if quick is not None:
        answer = quick.get("answer", "")
        actual_intent = quick.get("intent", intent)
    else:
        result = agent.handle_query(q, session_id=sid)
        answer = result.get("answer", "")
        actual_intent = result.get("intent", intent)
        tool_names = query_tool_logs(sid)
    _persist_turn(sid, q, answer, actual_intent)
    return {
        "question": q, "answer": answer, "actual_intent": actual_intent,
        "tools": tool_names, "actual_path": "router" if quick is not None else "agent",
        "latency": round(time.time() - started, 2),
    }


def _preload_history(sid: str, items: list):
    """RAG 评测：执行前把历史问答写入 query_history（模拟既有对话），供召回链路使用。"""
    import mysql_db
    for item in items:
        q = item if isinstance(item, str) else item.get("q", "")
        a = item.get("a", "历史测试回答") if isinstance(item, dict) else "历史测试回答"
        mysql_db.save_query_history(sid, q, a, "chat", 0.9)


def evaluate(agent, case: dict) -> dict:
    if "dialog" in case:
        return evaluate_dialog(agent, case)

    q = case["question"]
    sid = f"eval_{case['id']}"
    if case.get("preload_history"):
        _preload_history(sid, case["preload_history"])

    # 流式模式：走 handle_query_stream 收集全部 token，验证流式路径可用性
    if case.get("mode") == "stream":
        import asyncio
        _simulate_source(case)
        started = time.time()
        tokens: list[str] = []

        async def _collect():
            async for token in agent.handle_query_stream(q, session_id=sid):
                tokens.append(token)

        asyncio.run(_collect())
        answer = "".join(tokens)
        latency = round(time.time() - started, 2)
        failures = _score(case, "general", [], answer)
        return {
            "id": case["id"], "question": q, "mode": "stream",
            "expect_intent": case.get("expect_intent"),
            "actual_intent": "general", "actual_path": "stream",
            "tools": [], "latency": latency, "answer_len": len(answer),
            "answer": answer[:200], "pass": not failures, "failures": failures,
        }

    turn = _run_turn(agent, q, sid, case)
    failures = _score(case, turn["actual_intent"], turn["tools"], turn["answer"])
    return {
        "id": case["id"], "question": q, "mode": case.get("mode"),
        "expect_intent": case.get("expect_intent"),
        "actual_intent": turn["actual_intent"],
        "actual_path": turn["actual_path"], "tools": turn["tools"],
        "latency": turn["latency"], "answer_len": len(turn["answer"]),
        "answer": turn["answer"][:200], "pass": not failures, "failures": failures,
    }


def evaluate_dialog(agent, case: dict) -> dict:
    """多轮对话评测：同一会话按序执行 dialog，check_turn 轮做断言。

    支持 dialog 元素为字符串或 {"q", "repeat", "new_session"}：
    - repeat: 该问题重复 N 次（用于触发长会话压缩）
    - new_session: 该轮切换新会话（用于验证跨会话长期记忆注入）
    """
    turns_spec = case["dialog"]
    check_turn = case.get("check_turn", len(turns_spec))
    sid = f"eval_{case['id']}"
    logs: list[dict] = []
    turn_index = 0
    for spec in turns_spec:
        if isinstance(spec, dict):
            q = spec["q"]
            repeat = int(spec.get("repeat", 1))
            new_session = spec.get("new_session", False)
        else:
            q, repeat, new_session = spec, 1, False
        cur_sid = f"{sid}_b" if new_session else sid
        for _ in range(repeat):
            turn_index += 1
            turn = _run_turn(agent, q, cur_sid, case)
            turn["turn"] = turn_index
            turn["sid"] = cur_sid
            logs.append(turn)

    # 用 check_turn 轮的输出做断言（该轮的会话内工具日志仅统计本会话）
    target = logs[check_turn - 1] if 0 < check_turn <= len(logs) else logs[-1]
    failures = _score(case, target["actual_intent"], target["tools"], target["answer"])
    return {
        "id": case["id"], "question": f"[多轮×{len(logs)}] {logs[0]['question']}…",
        "mode": case.get("mode", "multi"), "expect_intent": case.get("expect_intent"),
        "actual_intent": target["actual_intent"], "actual_path": target["actual_path"],
        "tools": target["tools"], "latency": round(sum(t["latency"] for t in logs), 2),
        "answer_len": len(target["answer"]),
        "answer": target["answer"][:200], "pass": not failures, "failures": failures,
        "turns": len(logs), "check_turn": check_turn,
        "turn_logs": logs,
    }


def render_report(results: list[dict]) -> str:
    total = len(results)
    passed = sum(1 for r in results if r["pass"])
    router_cases = [r for r in results if r["mode"] == "router"]
    agent_cases = [r for r in results if r["mode"] == "agent"]
    multi_cases = [r for r in results if r["mode"] == "multi"]
    router_pass = sum(1 for r in router_cases if r["pass"])
    agent_pass = sum(1 for r in agent_cases if r["pass"])
    multi_pass = sum(1 for r in multi_cases if r["pass"])
    latencies = [r["latency"] for r in results]
    avg_lat = round(sum(latencies) / len(latencies), 2) if latencies else 0
    router_hit = sum(1 for r in results if r["actual_path"] == "router")
    total_turns = sum(r.get("turns", 1) for r in results)

    lines = [
        f"# Agent 评测报告 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- 用例总数: **{total}** | 通过: **{passed}** | 准确率: **{passed/total*100:.1f}%**",
        f"- 模板(零LLM): {router_pass}/{len(router_cases)} | Agent(LLM): {agent_pass}/{len(agent_cases)} | 多轮对话: {multi_pass}/{len(multi_cases)}",
        f"- 实际走模板拦截: {router_hit}/{total} ({router_hit/total*100:.0f}%) | 总轮次: {total_turns}",
        f"- 平均延迟: {avg_lat}s | 平均回答长度: {round(sum(r['answer_len'] for r in results)/max(total,1))} 字符",
        "",
        "## 明细",
        "",
        "| id | 模式 | 轮次 | 预期意图 | 实际意图 | 路径 | 工具 | 延迟 | 结果 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        mark = "✅" if r["pass"] else "❌"
        turns = f"{r.get('turns', 1)}" + (f"(查{r.get('check_turn')})" if r.get("turns") else "")
        lines.append(
            f"| {r['id']} | {r['mode']} | {turns} | {r['expect_intent'] or '-'} | "
            f"{r['actual_intent']} | {r['actual_path']} | "
            f"{','.join(r['tools']) or '-'} | {r['latency']}s | {mark} |"
        )
    fails = [r for r in results if not r["pass"]]
    if fails:
        lines += ["", "## 失败明细", ""]
        for r in fails:
            lines.append(f"**{r['id']}** `{r['question']}`")
            lines.append(f"- 失败原因: {'; '.join(r['failures'])}")
            lines.append(f"- 回答: {r['answer']}")
            if r.get("turn_logs"):
                lines.append(f"- 断言轮: 第 {r.get('check_turn')} 轮")
            lines.append("")
    return "\n".join(lines)


def _gha_annotate(level: str, title: str, message: str) -> None:
    """在 GitHub Actions 里发一条注解（Annotations 面板可见），本地跑时自动跳过。

    为什么要这个：CI 日志需要登录才能下载（匿名 API 403），
    而 **Annotations 面板是公开可见的** —— 把"哪条用例失败、为什么"直接写成注解，
    排查时不用再让人肉去翻日志（实测：连挂两轮，只拿到 "exit code 2" 这种信息量极低的提示）。
    `level`：error / warning / notice。
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    title = (title or "").replace("\n", " ")[:120]
    body = (message or "").replace("%", "%25").replace("\r", "").replace("\n", "%0A")[:1500]
    print(f"::{level} title={title}::{body}", flush=True)


def _gha_summary(text: str) -> None:
    """把报告写进 Actions 的 Job Summary（公开可见），方便不下载日志也能看结论。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception as e:
        print(f"[CI] 写 Job Summary 失败(可忽略): {e}")


def _print_env_banner() -> None:
    """打印运行环境与关键依赖版本 —— CI 上"本地能跑、CI 挂"时的第一手线索。

    动机（实测）：CI 只给一个 `exit code 2`，日志还要登录才能下载；
    而最常见的原因就是**依赖版本漂移**（requirements.txt 用的是下界 `>=`，
    CI 每次解析到的 langchain/langgraph/openai 版本可能比容器锁版更新）。
    把版本号打出来，"本地 1.3.14 / CI 1.5.0"这种差异一眼就能看见。
    """
    import platform
    print(f"[env] Python {platform.python_version()} | {platform.system()} {platform.release()}")
    for mod in ("langchain", "langchain-core", "langgraph", "langchain-openai",
                "openai", "qdrant-client", "pymysql", "pyyaml", "langfuse"):
        try:
            import importlib.metadata as md
            print(f"[env]   {mod:16s} {md.version(mod)}")
        except Exception as e:
            print(f"[env]   {mod:16s} 不可用: {type(e).__name__}")
    try:
        import mysql_db
        conn = mysql_db.get_connection()
        conn.close()
        print(f"[env]   MySQL 可连接 (db={mysql_db.MYSQL_DB})")
    except Exception as e:
        print(f"[env]   MySQL 连接失败: {type(e).__name__}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Agent 评测集跑分")
    parser.add_argument("--case", default=None,
                        help="只跑指定用例 id（调试；支持逗号分隔多个，如 --case pop_01,emo_01）")
    parser.add_argument("--file", default=None,
                        help="用例文件（默认 evals/cases.json）。"
                             "长会话记忆套件：--file evals/cases_memory.json")
    args = parser.parse_args()

    cases = load_cases(args.file)
    if args.case:
        wanted = [c.strip() for c in args.case.split(",") if c.strip()]
        cases = [c for c in cases if c["id"] in wanted]
        if not cases:
            print(f"未找到用例 {args.case}")
            sys.exit(1)

    print(f"加载 {len(cases)} 条用例，装配 Agent（真实技能 + DeepSeek）...")
    _print_env_banner()
    try:
        agent = get_agent()
    except Exception as e:
        # 装配失败 = 环境问题（缺依赖/DB 连不上/key 无效），单独报出来，
        # 不要和"用例断言失败"混在一起（CI 上这两类红的处理方式完全不同）
        _gha_annotate("error", "Agent 装配失败（环境问题，不是用例失败）",
                      f"{type(e).__name__}: {e}")
        raise
    print("[阶段] Agent 装配完成，开始跑用例")

    results = []
    for i, case in enumerate(cases, 1):
        try:
            r = evaluate(agent, case)
        except Exception as e:
            # 单条用例崩溃不再"一票否决整个跑分"：记成一条失败用例继续，
            # 否则一个坏用例会把其余 84 条的结论全部藏起来（CI 里尤其致命）
            import traceback
            r = {
                "id": case.get("id", f"case_{i}"), "question": case.get("question", ""),
                "mode": case.get("mode", "?"), "expect_intent": case.get("expect_intent"),
                "actual_intent": "exception", "actual_path": "exception",
                "tools": [], "latency": 0.0, "answer_len": 0,
                "answer": f"{type(e).__name__}: {e}",
                "pass": False, "failures": [f"用例执行异常: {type(e).__name__}: {e}"],
                "traceback": traceback.format_exc(),
            }
            if len([x for x in results if x.get("actual_path") == "exception"]) < 3:
                _gha_annotate("error", f"用例崩溃 {r['id']}",
                              f"{type(e).__name__}: {e}\n\n{r['traceback'][-800:]}")
        results.append(r)
        mark = "✅" if r["pass"] else "❌"
        print(
            f"[{i}/{len(cases)}] {mark} {r['id']} "
            f"({r['mode']}/{r['actual_path']}, {r['latency']}s"
            + (f", tools={r['tools']}" if r["tools"] else "")
            + (f", 原因: {'; '.join(r['failures'])}" if r["failures"] else "")
            + ")"
        )

    report = render_report(results)
    print("\n" + "=" * 60)
    print(report)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = REPORTS_DIR / f"run_{ts}.md"
    out.write_text(report, encoding="utf-8")
    print(f"\n报告已保存: {out}")

    # ===== LLM 用量快照：by_tag.summary 的条数即"长会话压缩真的触发过"的硬证据 =====
    try:
        from agents.llm_metrics import get_llm_metrics
        snap = get_llm_metrics().snapshot()
        by_tag = snap.get("by_tag", {})
        print("\n" + "-" * 60)
        print(f"[LLM] 调用 {snap.get('calls')} 次 | 失败 {snap.get('errors')} 次 | "
              f"token 入 {snap.get('prompt_tokens')} / 出 {snap.get('completion_tokens')} | "
              f"无 usage 上报 {snap.get('calls_without_usage')} 次")
        if snap.get("latency_ms", {}).get("samples"):
            lat = snap["latency_ms"]
            print(f"[LLM] 延迟 平均 {lat['avg']}ms / p95 {lat['p95']}ms / 最大 {lat['max']}ms "
                  f"（样本 {lat['samples']}）")
        toks = snap.get("tokens_by_tag", {})
        for tag, cnt in by_tag.items():
            tk = toks.get(tag, {})
            print(f"[LLM]   {tag:16s} 调用 {cnt:3d} | 入 {tk.get('prompt', 0):6d} "
                  f"出 {tk.get('completion', 0):5d} token")
        if not by_tag.get("summary"):
            print("[LLM] 注意：本次没有 summary 调用 → 长会话压缩未触发（断言只覆盖了上下文窗口内）")
        print("-" * 60)
    except Exception as e:
        print(f"[LLM] 指标读取失败(不影响评测结论): {e}")

    # ===== CI 门禁：任一用例失败 → 非零退出码（供流水线拦截回归） =====
    passed = sum(1 for r in results if r["pass"])
    failed = [r for r in results if not r["pass"]]
    for r in failed[:15]:                     # 注解有数量上限，先报前 15 条
        _gha_annotate("error", f"eval 失败 {r['id']}",
                      f"问题: {r['question']}\n原因: {'; '.join(r['failures'])}\n"
                      f"路径: {r['actual_path']} 意图: {r['actual_intent']} 工具: {r['tools'] or '无'}\n"
                      f"回答: {r['answer'][:300]}")
    if len(failed) > 15:
        _gha_annotate("warning", "eval 失败过多", f"共 {len(failed)} 条失败，仅前 15 条有注解")
    _gha_summary(report)
    if failed:
        print(f"\n[CI-GATE] 评测失败 {len(failed)}/{len(results)} 条，退出码 1（回归拦截）")
        sys.exit(1)
    print(f"[CI-GATE] 评测全部通过 {passed}/{len(results)}，退出码 0")


if __name__ == "__main__":
    # 必须先给默认值：main() 全绿时**不抛 SystemExit**（只打印 CI-GATE），
    # 原来的写法会让 exit_code 从未赋值 → 结尾 sys.exit(exit_code) 抛
    # NameError（实测：单跑一条全通过的用例 `--case mem_ctx_01` 就崩在最后一行）
    exit_code = 0
    try:
        main()
    except SystemExit as e:
        exit_code = e.code or 0
    except Exception as e:
        # 异常必须**连栈一起打**：CI 日志需要登录才能读，只给一行 "exit code 2"
        # 根本没法定位（实测连挂两轮都是这样）。同时发一条注解 + 写 Job Summary。
        import traceback
        tb = traceback.format_exc()
        print(f"[CI-GATE] 评测异常: {type(e).__name__}: {e}")
        print(tb)
        _gha_annotate("error", f"评测异常 {type(e).__name__}", f"{e}\n\n关键栈帧:\n" + tb[-1200:])
        _gha_summary(f"## ❌ 评测异常（退出码 2）\n\n```\n{tb[-2000:]}\n```\n")
        exit_code = 2
    finally:
        # 清理评测会话的工具日志与检查点（检查点残留会让下次跑不是冷启动，评测不可重复）
        import mysql_db
        try:
            conn = mysql_db.get_connection()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_tool_log WHERE session_id LIKE 'eval_%'")
                cur.execute("DELETE FROM agent_checkpoints WHERE thread_id LIKE 'eval_%'")
                cur.execute("DELETE FROM agent_checkpoint_writes WHERE thread_id LIKE 'eval_%'")
                cur.execute("DELETE FROM agent_long_term_memory WHERE session_id LIKE 'eval_%'")
                cur.execute("DELETE FROM query_history WHERE session_id LIKE 'eval_%'")
                cur.execute("DELETE FROM chat_session WHERE session_id LIKE 'eval_%'")
            conn.close()
            print("[cleanup] 评测工具日志/检查点已清理")
        except Exception as e:
            print(f"[cleanup] 清理失败(可忽略): {e}")
    sys.exit(exit_code)
