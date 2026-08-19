"""
主动汇报 Agent — 定期生成自然语言运营汇报 + 异常突增检测与主动推送

- 常规汇报：每 HEAT_REPORT_INTERVAL_SECONDS 用 LLM 生成运营摘要（失败回退模板），
  写入 heat_report 表并通过 WebSocket 广播
- 突增检测：周期性对比告警水位，高风险/总告警突增时立即生成"异常突增"汇报并推送
"""
import threading
import time
from datetime import datetime
from typing import Any

# 进程内基线：上次观测到的告警水位
_baseline = {"total_alerts": 0, "high_risk_count": 0, "total_visits": 0}
_baseline_lock = threading.Lock()

# 突增阈值
SURGE_HIGH_DELTA = 2   # 高风险告警增量 >= 2 → 突增
SURGE_TOTAL_DELTA = 5  # 总告警增量 >= 5 且当前有高风险 → 突增
# 突增检测周期（秒）
CHECK_INTERVAL = 60

# 汇报文案中禁止使用的法律定性词
_FORBIDDEN = ("偷窃", "盗窃", "小偷", "盗")


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


def _template_text(pop: dict, anom: dict, emo: dict, trend: dict, report_type: str = "regular") -> str:
    """模板汇报（LLM 不可用时的降级）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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


def _llm_text(pop: dict, anom: dict, emo: dict, trend: dict, report_type: str) -> str:
    """用 LLM 生成自然语言运营汇报；失败回退模板。"""
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
        prompt = (
            "你是超市零售运营分析助手。请根据以下实时监控数据生成一段简短的运营汇报"
            f"（3~5句话，中文，面向店长）：\n"
            f"【货架热度】{'; '.join(zone_lines) or '暂无'}\n"
            f"【告警】总 {anom.get('total_alerts', 0)} 起，高风险 {anom.get('high_risk_count', 0)} 起\n"
            f"【表情】识别 {emo.get('total_faces', 0)} 人次，正面 {emo.get('positive_count', 0)}，"
            f"负面 {emo.get('negative_count', 0)}，趋势：{trend.get('conclusion', '')}\n"
            f"要求：{focus} 使用中性措辞，禁止使用偷窃、盗窃等法律定性词汇；"
            "若存在高风险告警必须注明\"建议人工复核\"。"
        )
        resp = create_llm(temperature=0.3).invoke(prompt)
        text = (resp.content or "").strip() if isinstance(resp.content, str) else str(resp.content or "").strip()
        if text:
            return _sanitize(text)
    except Exception as e:
        print(f"[ReportAgent] LLM 生成失败，回退模板: {e}")
    return _template_text(pop, anom, emo, trend, report_type)


def generate_regular_report() -> dict:
    """生成一轮常规运营汇报。返回 {"summary": str, "data": dict}。"""
    pop, anom, emo, trend = _collect_snapshot()
    summary = _llm_text(pop, anom, emo, trend, "regular")
    return {
        "summary": summary,
        "data": {
            "type": "regular",
            "generator": "agent",
            "top_zone": pop.get("top_zone"),
            "total_visits": pop.get("total_visits", 0),
            "total_alerts": anom.get("total_alerts", 0),
            "high_risk_count": anom.get("high_risk_count", 0),
            "stats": pop,
            "timestamp": datetime.now().isoformat(),
        },
    }


def detect_surge() -> dict | None:
    """对比基线检测异常突增。返回突增信息 dict 或 None。"""
    _, anom, _, _ = _collect_snapshot()
    total = anom.get("total_alerts", 0)
    high = anom.get("high_risk_count", 0)
    with _baseline_lock:
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


def generate_surge_report(surge: dict) -> dict:
    """生成"异常突增"汇报。"""
    pop, anom, emo, trend = _collect_snapshot()
    summary = _llm_text(pop, anom, emo, trend, "surge")
    return {
        "summary": summary,
        "data": {
            "type": "surge",
            "generator": "agent",
            "top_zone": pop.get("top_zone"),
            "total_visits": pop.get("total_visits", 0),
            "total_alerts": surge.get("total_alerts", 0),
            "high_risk_count": surge.get("high_risk_count", 0),
            "delta_total": surge.get("delta_total", 0),
            "delta_high": surge.get("delta_high", 0),
            "timestamp": datetime.now().isoformat(),
        },
    }


def update_baseline(anom_summary: dict):
    """用当前告警水位刷新基线。"""
    with _baseline_lock:
        _baseline["total_alerts"] = anom_summary.get("total_alerts", 0)
        _baseline["high_risk_count"] = anom_summary.get("high_risk_count", 0)


def reset_baseline():
    with _baseline_lock:
        _baseline.update({"total_alerts": 0, "high_risk_count": 0, "total_visits": 0})
