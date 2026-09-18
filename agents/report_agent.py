"""
主动汇报 Agent — 由 LangGraph StateGraph 编排的汇报工作流

图结构（显式节点 + 条件分支 + 降级）：

    START → collect → generate ──LLM 成功──→ sanitize → finalize → END
                             └──LLM 失败──→ template ↗

- collect   ：采集热度/告警/表情实时快照
- generate  ：调用 LLM 生成自然语言汇报
- template  ：降级分支（LLM 不可用/异常时用模板拼接，保证链路不断）
- sanitize  ：合规过滤（法律定性词 → "可疑行为"）
- finalize  ：组装结构化数据（payload 与旧版逐字段一致）

业务行为：
- 常规汇报：每 HEAT_REPORT_INTERVAL_SECONDS 生成运营摘要，写入 heat_report 表并经 WebSocket 广播
- 突增检测：周期性对比告警水位，高风险/总告警突增时立即生成"异常突增"汇报并推送

对外接口与旧版保持一致（generate_regular_report / generate_surge_report /
detect_surge / update_baseline / reset_baseline），调用方（scheduled_tasks）无需改动。
"""
import threading
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agents import data_quality

# 进程内基线：上次观测到的告警水位
#
# ⚠ `ready` 用来避免**重启后的第一次假突增**：
# 原先 `_baseline` 初值是 0，重启后第一次 detect_surge() 会拿"历史累计水位"
# 与 0 相减，于是必然报一次突增（实测重启首轮 delta_total=30、delta_high=8）。
# 现在首轮只**建立基线**、不判定，从第二轮起才比较。
# 另注：基线存在进程内，多副本部署时每个副本各有一套（见 改进记录.md 待办 F7）。
_baseline = {"total_alerts": 0, "high_risk_count": 0, "total_visits": 0, "ready": False}
_baseline_lock = threading.Lock()

# 突增阈值
SURGE_HIGH_DELTA = 2   # 高风险告警增量 >= 2 → 突增
SURGE_TOTAL_DELTA = 5  # 总告警增量 >= 5 且当前有高风险 → 突增
# 突增检测周期（秒）
CHECK_INTERVAL = 60

# 汇报文案中禁止使用的法律定性词
_FORBIDDEN = ("偷窃", "盗窃", "小偷", "盗")


# ==================== 工作流状态 ====================

class ReportState(TypedDict, total=False):
    """汇报工作流共享状态（节点间传递）。"""
    report_type: str    # "regular" | "surge"
    surge: dict         # 突增信息（report_type="surge" 时使用）
    pop: dict           # 货架热度快照
    anom: dict          # 告警快照
    emo: dict           # 表情快照
    trend: dict         # 表情趋势
    llm_failed: bool    # LLM 是否失败（决定走模板降级分支）
    llm_skipped: bool   # 是否因"无变化"被成本门控跳过（台账 F5）
    force_llm: bool     # 强制调用 LLM（绕过变化门控）
    skip_reason: str    # 门控判定原因（用于日志/指标）
    quality: dict       # 数据可信度快照（门禁：区分「无数据」与「无客流」）
    summary: str        # 最终文案
    data: dict          # 最终结构化数据


# ==================== 基础工具 ====================

def _sanitize(text: str) -> str:
    for w in _FORBIDDEN:
        text = text.replace(w, "可疑行为")
    return text


def _collect_snapshot() -> tuple:
    """收集热度/告警/表情实时数据（懒加载 skill 单例，后台线程安全）。"""
    from api.dependencies import get_popularity_skill, get_anomaly_skill, get_emotion_skill

    pop = get_popularity_skill().get_stats()
    anom = get_anomaly_skill().get_alert_summary()
    emo = get_emotion_skill().get_stats()
    trend = get_emotion_skill().get_trend()
    return pop, anom, emo, trend


def _template_text(pop: dict, anom: dict, emo: dict, trend: dict,
                   report_type: str = "regular", quality: dict | None = None) -> str:
    """模板汇报（LLM 不可用时的降级）。

    数据不可信（视频源未启动/断流）时**不输出「到访 0 人次」这类会误导的结论**，
    而是明确说明当前无有效数据——避免把设备故障报成「今天没生意」。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # ⚠ 门禁按**零售源**判定，不能用聚合口径的 quality["reliable"]：
    # 聚合口径是"任一路新鲜即为真"，于是浏览器推帧（client）会把停摆的
    # 服务器摄像头掩盖成"数据可信"，汇报照样输出"到访 0 人次"
    # （见 agents/data_quality.py 的说明）。
    # 兼容：若调用方传进来的 quality 没有 reliable_by_source 字段，退回旧口径。
    if quality is not None:
        by_src = quality.get("reliable_by_source")
        reliable = by_src.get("retail") if isinstance(by_src, dict) else None
        if reliable is None:
            reliable = quality.get("reliable", True)
        if not reliable:
            reason = quality.get("reason") or "服务器摄像头当前没有视频帧，本次结论依赖它"
            if report_type == "surge":
                return (
                    f"【异常突增·数据不可信】{now}：{reason}，"
                    "告警增量无法确认，请先检查摄像头再判断是否需人工复核。"
                )
            return f"【数据不可信】{now}：{reason}。本次不出运营结论，请检查摄像头/视频源后重试。"

    zones = pop.get("zones", {})
    top = pop.get("top_zone")
    top_label = (zones.get(top, {}) or {}).get("zone_label", top or "无") if top else "无"
    total_visits = pop.get("total_visits", 0)
    total_staff = pop.get("total_staff", 0)
    total_alerts = anom.get("total_alerts", 0)
    high = anom.get("high_risk_count", 0)
    faces = emo.get("total_faces", 0)

    if report_type == "surge":
        head = f"【异常突增】{now}：当前高风险告警 {high} 起（较上次明显增加），需立即人工复核。"
    else:
        head = f"热度汇报 {now}："
    return (
        head
        + f"最热区域 {top_label}，累计到访 {total_visits} 人次，疑似店员 {total_staff} 人；"
        + f"告警 {total_alerts} 起（高风险 {high} 起）；识别表情 {faces} 人次。"
        + ("建议人工复核高风险告警。" if high > 0 else "")
    )


def _try_llm_text(pop: dict, anom: dict, emo: dict, trend: dict, report_type: str,
                  quality: dict | None = None) -> str | None:
    """用 LLM 生成自然语言运营汇报；失败返回 None（由条件边转模板降级分支）。"""
    try:
        from agents.base_agent import create_llm

        zones = pop.get("zones", {})
        zone_lines = []
        for z in sorted(zones.values(), key=lambda x: x.get("heat_score", 0), reverse=True)[:3]:
            zone_lines.append(
                f"{z.get('zone_label', z.get('zone_id', ''))}: 到访{z.get('visit_count', 0)}人次,"
                f"停留{z.get('total_dwell_seconds', 0)}秒, 热度{z.get('heat_score', 0)}"
            )
        focus = "检测到高风险告警突增，请重点说明异常情况并强调需人工复核。" if report_type == "surge" else "请客观汇报整体运营情况。"

        # 数据可信度门禁：数据不可信时必须说明「无有效数据」，禁止把 0 解读成业务结论
        if quality is not None and not quality.get("reliable", True):
            quality_note = (
                "\n【数据可信度】当前实时数据**不可信**"
                f"（{quality.get('reason', '无有效视频数据')}）。"
                "上述各项 0 值是「没有采集到数据」而非「真的没有客流/告警」。"
                "请明确告知店长：本次无法给出运营结论，并提示检查摄像头/视频源是否正常，"
                "**不要**基于这些 0 值分析客流量、顾客情绪或经营好坏。\n"
            )
        else:
            quality_note = "\n【数据可信度】实时数据正常。\n"

        prompt = (
            "你是超市零售运营分析助手。请根据以下实时监控数据生成一段简短的运营汇报"
            f"（3~5句话，中文，面向店长）：\n"
            f"【货架热度】{'; '.join(zone_lines) or '暂无'}\n"
            f"【告警】总 {anom.get('total_alerts', 0)} 起，高风险 {anom.get('high_risk_count', 0)} 起\n"
            f"【表情】识别 {emo.get('total_faces', 0)} 人次，正面 {emo.get('positive_count', 0)}，"
            f"负面 {emo.get('negative_count', 0)}，趋势：{trend.get('conclusion', '')}\n"
            f"{quality_note}"
            f"要求：{focus} 使用中性措辞，禁止使用偷窃、盗窃等法律定性词汇；"
            "若存在高风险告警必须注明\"建议人工复核\"。"
        )
        resp = create_llm(temperature=0.3, tag="report").invoke(prompt)
        text = (resp.content or "").strip() if isinstance(resp.content, str) else str(resp.content or "").strip()
        if text:
            return text
    except Exception as e:
        print(f"[ReportAgent] LLM 生成失败，走模板降级分支: {e}")
    return None


# ==================== 图节点 ====================

def _node_collect(state: ReportState) -> dict:
    """节点 1：采集实时数据快照 + 数据可信度快照。"""
    pop, anom, emo, trend = _collect_snapshot()
    return {"pop": pop, "anom": anom, "emo": emo, "trend": trend,
            "quality": data_quality.snapshot()}


def _node_generate(state: ReportState) -> dict:
    """节点 2：LLM 生成汇报文案；失败置 llm_failed，由条件边转模板分支。

    ⚠ **变化门控（台账 F5）**：定时汇报固定 10 分钟一轮 = 144 次/天/店，
    而没有变化时那些调用只是把同样的数字重新措辞一遍 ✗。这里先比业务签名：
    无变化 → 直接走**模板降级分支**（不调 LLM，仍有文字入库/推送 ✓）；
    有变化 / 首次 / 可信度翻转 / 强制（异常突增）/ 静默超时 → 正常调 LLM ✓。
    """
    from agents.report_change_gate import get_report_gate, signature

    gate = get_report_gate()
    sig = signature(state.get("pop"), state.get("anom"), state.get("emo"), state.get("quality"))
    force = bool(state.get("force_llm")) or state.get("report_type") == "surge"
    should_call, reason = gate.decide(sig, force=force)
    if not should_call:
        gate.commit(sig, called_llm=False, reason=reason)
        print(f"[Report] 跳过 LLM 汇报（成本门控）：{reason}")
        return {"llm_failed": True, "llm_skipped": True, "skip_reason": reason}

    text = _try_llm_text(
        state.get("pop") or {},
        state.get("anom") or {},
        state.get("emo") or {},
        state.get("trend") or {},
        state.get("report_type", "regular"),
        state.get("quality"),
    )
    gate.commit(sig, called_llm=bool(text), reason=reason)
    if text:
        return {"summary": text, "llm_failed": False, "llm_skipped": False, "skip_reason": reason}
    return {"llm_failed": True, "llm_skipped": False, "skip_reason": reason}


def _route_after_generate(state: ReportState) -> str:
    """条件边：LLM 成功 → 合规过滤；失败 → 模板降级。"""
    return "fallback" if state.get("llm_failed") else "llm"


def _node_template(state: ReportState) -> dict:
    """节点 3b（降级分支）：模板拼接汇报。"""
    return {
        "summary": _template_text(
            state.get("pop") or {},
            state.get("anom") or {},
            state.get("emo") or {},
            state.get("trend") or {},
            state.get("report_type", "regular"),
            state.get("quality"),
        ),
        "llm_failed": True,
    }


def _node_sanitize(state: ReportState) -> dict:
    """节点 4：合规过滤（法律定性词 → 可疑行为）。"""
    return {"summary": _sanitize(state.get("summary", "") or "")}


def _node_finalize(state: ReportState) -> dict:
    """节点 5：组装结构化数据（字段与旧版逐一对齐）。"""
    rtype = state.get("report_type", "regular")
    pop = state.get("pop") or {}
    anom = state.get("anom") or {}
    surge = state.get("surge") or {}

    if rtype == "surge":
        data = {
            "type": "surge",
            "generator": "agent",
            "top_zone": pop.get("top_zone"),
            "total_visits": pop.get("total_visits", 0),
            "total_alerts": surge.get("total_alerts", 0),
            "high_risk_count": surge.get("high_risk_count", 0),
            "delta_total": surge.get("delta_total", 0),
            "delta_high": surge.get("delta_high", 0),
            "data_quality": state.get("quality") or {},
            "timestamp": datetime.now().isoformat(),
        }
    else:
        data = {
            "type": "regular",
            "generator": "agent",
            "top_zone": pop.get("top_zone"),
            "total_visits": pop.get("total_visits", 0),
            "total_alerts": anom.get("total_alerts", 0),
            "high_risk_count": anom.get("high_risk_count", 0),
            "stats": pop,
            "data_quality": state.get("quality") or {},
            "timestamp": datetime.now().isoformat(),
        }
    return {"data": data}


def _build_graph():
    """构建汇报工作流图（编译一次，进程内复用）。"""
    builder = StateGraph(ReportState)
    builder.add_node("collect", _node_collect)
    builder.add_node("generate", _node_generate)
    builder.add_node("template", _node_template)
    builder.add_node("sanitize", _node_sanitize)
    builder.add_node("finalize", _node_finalize)

    builder.add_edge(START, "collect")
    builder.add_edge("collect", "generate")
    builder.add_conditional_edges(
        "generate",
        _route_after_generate,
        {"llm": "sanitize", "fallback": "template"},
    )
    builder.add_edge("template", "sanitize")
    builder.add_edge("sanitize", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


_report_graph = _build_graph()


# ==================== 对外接口（与旧版签名/返回结构一致） ====================

def generate_regular_report() -> dict:
    """生成一轮常规运营汇报（走 StateGraph 工作流）。返回 {"summary": str, "data": dict}。"""
    out = _report_graph.invoke({"report_type": "regular"})
    return {"summary": out.get("summary", ""), "data": out.get("data", {})}


def generate_surge_report(surge: dict) -> dict:
    """生成"异常突增"汇报（走 StateGraph 工作流）。"""
    out = _report_graph.invoke({"report_type": "surge", "surge": surge or {}})
    return {"summary": out.get("summary", ""), "data": out.get("data", {})}


def _alert_levels(anom: dict) -> tuple[int, int]:
    """取告警水位（优先**累计**口径）。

    累计口径不封顶，是增量判定的正确依据；旧字段（明细条数）受队列上限 500 约束，
    一旦封顶增量恒为 0、突增永远不触发（见 skills/skill_anomaly.py 的说明）。
    兼容：没有新字段时退回旧字段。
    """
    total = anom.get("total_alerts_cumulative", anom.get("total_alerts", 0))
    high = anom.get("high_risk_count_cumulative", anom.get("high_risk_count", 0))
    return int(total or 0), int(high or 0)


def detect_surge() -> dict | None:
    """对比基线检测异常突增。返回突增信息 dict 或 None。

    首轮**只建立基线、不判定**——否则会把"本次启动之前累积的历史水位"
    当成一轮突增报出去（重启即误报）。
    """
    _, anom, _, _ = _collect_snapshot()
    total, high = _alert_levels(anom)
    with _baseline_lock:
        if not _baseline["ready"]:
            _baseline["total_alerts"] = total
            _baseline["high_risk_count"] = high
            _baseline["ready"] = True
            return None
        base_total = _baseline["total_alerts"]
        base_high = _baseline["high_risk_count"]
    d_total = total - base_total
    d_high = high - base_high
    if d_high >= SURGE_HIGH_DELTA or (d_total >= SURGE_TOTAL_DELTA and high > 0):
        return {
            "total_alerts": total,
            "high_risk_count": high,
            "delta_total": d_total,
            "delta_high": d_high,
        }
    return None


def update_baseline(anom_summary: dict):
    """用当前告警水位刷新基线（用累计口径，见 _alert_levels）。"""
    total, high = _alert_levels(anom_summary or {})
    with _baseline_lock:
        _baseline["total_alerts"] = total
        _baseline["high_risk_count"] = high
        _baseline["ready"] = True


def reset_baseline():
    with _baseline_lock:
        _baseline.update({"total_alerts": 0, "high_risk_count": 0,
                          "total_visits": 0, "ready": False})
