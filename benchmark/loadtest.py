"""
并发压测脚本 — POST /api/queries（智能零售问答接口）

方法学：
- 客户端 asyncio + httpx.AsyncClient，信号量控制并发数，模拟真实多用户
- 预热：每轮先发 N 个请求（避免首请求冷启动污染延迟分布）
- 指标：QPS（总请求/总耗时）、P50/P90/P95/P99 延迟分位、错误率
- 三类场景：
    template  → 模板路由快路径（零 LLM，验证毫秒级 + 高并发 QPS）
    agent     → 完整 Agent 路径（LLM + 工具编排，验证异步并发与吞吐）
    mixed     → 混合流量（模板为主，少量 Agent，验证整体稳定性）
- 会话池：固定 N 个 bench_* session_id，模拟真实用户复用会话

用法:
    python benchmark/loadtest.py --mode template --concurrency 200 --requests 1500
    python benchmark/loadtest.py --all           # 跑完整矩阵
"""
import argparse
import asyncio
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8000"
REPORT_DIR = Path(__file__).resolve().parent / "reports"

# 模板问题池（来自评测集 router 用例，保证走模板路由：强词命中 + 无分析结构。
# 注意：不能混入"那热度呢"这类弱词单独问题——保守化设计会让它走 LLM，
# 混入会污染"纯模板"压测统计）
TEMPLATE_QUESTIONS = [
    "1号货架现在客流怎么样",
    "货架排名",
    "告警有哪些",
    "顾客情绪怎么样",
    "最近的热度汇报",
    "3号货架呢",
    "今天有没有可疑行为",
    "哪个区域最受欢迎",
    "1号货架",
    "货架热毒怎么样",
]

# Agent 问题池（走完整 LLM 流程：工具编排 + 推理）
AGENT_QUESTIONS = [
    "帮我看看现在门店的整体运营情况，逐项说明",
    "综合评估一下当前门店的整体运营情况",
    "今天客流为什么这么少？帮我分析一下原因",
    "顾客逛完1号货架后最常去哪个区域？怎么陈列比较好？",
    "哪个区域顾客停留很久却不买？可能是什么问题？",
    "今天几点是客流高峰？补货应该安排在什么时段？",
]


def percentile(sorted_lat: list[float], p: float) -> float:
    """纯 Python 分位（不依赖 numpy）。"""
    if not sorted_lat:
        return 0.0
    idx = min(len(sorted_lat) - 1, int(p / 100 * len(sorted_lat)))
    return round(sorted_lat[idx], 3)


def summarize(lats: list[float], total_elapsed: float) -> dict:
    s = sorted(lats)
    n = len(s)
    qps = n / total_elapsed if total_elapsed > 0 else 0.0
    return {
        "requests": n,
        "qps": round(qps, 1),
        "avg_ms": round(sum(s) / n * 1000, 2) if n else 0,
        "min_ms": round(s[0] * 1000, 2) if s else 0,
        "max_ms": round(s[-1] * 1000, 2) if s else 0,
        "p50_ms": percentile(s, 50),
        "p90_ms": percentile(s, 90),
        "p95_ms": percentile(s, 95),
        "p99_ms": percentile(s, 99),
        "errors": 0,
    }


async def _fire(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    questions: list[str],
    session_pool: list[str],
    results: list[tuple[float, int]],
    mode: str,
):
    async with sem:
        q = random.choice(questions)
        # Agent 路径每请求独立 session：同 session 并发会破坏 LangGraph 检查点
        # 状态（压测实证：tool 消息 400 "must be a response to tool_calls"）
        is_agent_q = (mode == "agent") or (mode == "mixed" and q in AGENT_QUESTIONS)
        if is_agent_q:
            sid = f"bench_{mode}_{random.randint(0, 999999)}"
        else:
            sid = random.choice(session_pool)
        t0 = time.perf_counter()
        try:
            r = await client.post(
                f"{BASE_URL}/api/queries",
                json={"question": q, "session_id": sid},
                timeout=120,
            )
            status = r.status_code
        except Exception:
            status = -1
        results.append((time.perf_counter() - t0, status))


async def run_round(
    name: str,
    mode: str,
    concurrency: int,
    total: int,
    session_pool_size: int = 20,
    warmup: int = 10,
) -> dict:
    if mode == "agent":
        questions = AGENT_QUESTIONS
    elif mode == "mixed":
        questions = TEMPLATE_QUESTIONS * 9 + AGENT_QUESTIONS  # 模板为主（约 90/10）
    else:
        questions = TEMPLATE_QUESTIONS
    session_pool = [f"bench_{mode}_{i}" for i in range(session_pool_size)]
    results: list[tuple[float, int]] = []
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        # 预热（模板：首个请求 skill 懒加载；agent：会话/模型冷启动）
        for _ in range(min(warmup, total)):
            await _fire(client, sem, questions, session_pool, results, mode)

        started = time.perf_counter()
        tasks = [asyncio.create_task(_fire(client, sem, questions, session_pool, results, mode))
                 for _ in range(total)]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started

    # 去掉预热样本（前 warmup 个；total < warmup 时按 total 计，避免切片越界）
    start_idx = min(warmup, total)
    lats = [lat for lat, st in results[start_idx:] if st == 200]
    errors = [st for lat, st in results[start_idx:] if st != 200]
    summary = summarize(lats, elapsed)
    summary["errors"] = len(errors)
    summary["error_rate"] = round(len(errors) / max(total, 1) * 100, 2)
    summary["name"] = name
    summary["mode"] = mode
    summary["concurrency"] = concurrency
    summary["total"] = total
    summary["elapsed_s"] = round(elapsed, 2)
    return summary


def render_report(rounds: list[dict]) -> str:
    lines = [
        f"# 并发压测报告 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- 目标接口: `POST /api/queries`（模板快路径 + Agent LLM 路径）",
        f"- 压测方法: asyncio+httpx 客户端并发（信号量限流），每轮预热后计时",
        "",
        "## 结果矩阵",
        "",
        "| 场景 | 并发 | 请求数 | QPS | 平均 | P50 | P90 | P95 | P99 | 最大 | 错误率 | 耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rounds:
        lines.append(
            f"| {r['name']} | {r['concurrency']} | {r['total']} | {r['qps']} | "
            f"{r['avg_ms']}ms | {r['p50_ms']}ms | {r['p90_ms']}ms | {r['p95_ms']}ms | "
            f"{r['p99_ms']}ms | {r['max_ms']}ms | {r['error_rate']}% | {r['elapsed_s']}s |"
        )
    lines += ["", "## 结论要点", ""]
    lines.append("（由压测后人工/Agent 补充：QPS 上限、延迟分位是否符合预期、瓶颈分析）")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="POST /api/queries 并发压测")
    parser.add_argument("--all", action="store_true", help="跑完整矩阵")
    parser.add_argument("--mode", choices=["template", "agent", "mixed"], default=None)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--requests", type=int, default=1000)
    args = parser.parse_args()

    async def _main():
        rounds: list[dict] = []
        if args.all:
            rounds.append(await run_round("模板-低并发", "template", 20, 200))
            rounds.append(await run_round("模板-中并发", "template", 100, 1000))
            rounds.append(await run_round("模板-高并发", "template", 300, 1500))
            rounds.append(await run_round("Agent-并发3", "agent", 3, 6))
            rounds.append(await run_round("混合-模板为主", "mixed", 30, 120))
        else:
            mode = args.mode or "template"
            rounds.append(await run_round(f"{mode}-c{args.concurrency}", mode, args.concurrency, args.requests))
        for r in rounds:
            print(
                f"[{r['name']}] 并发={r['concurrency']} 请求={r['total']} "
                f"QPS={r['qps']} P50={r['p50_ms']}ms P95={r['p95_ms']}ms "
                f"P99={r['p99_ms']}ms 错误={r['error_rate']}% 耗时={r['elapsed_s']}s"
            )
        report = render_report(rounds)
        print("\n" + "=" * 60 + "\n" + report)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / f"loadtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        out.write_text(report, encoding="utf-8")
        print(f"\n报告已保存: {out}")

    asyncio.run(_main())


if __name__ == "__main__":
    main()
