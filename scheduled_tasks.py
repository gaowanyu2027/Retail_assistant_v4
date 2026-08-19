"""定期任务：主动汇报 Agent

- 启动后立即生成一轮常规汇报（便于验证）
- 周期性（HEAT_REPORT_INTERVAL_SECONDS）生成 LLM 运营汇报并入库 + WebSocket 广播
- 周期性（CHECK_INTERVAL）检测异常突增，突增时立即生成"异常突增"汇报并推送
"""
import threading
import time
from datetime import datetime

from config.settings import HEAT_REPORT_INTERVAL_SECONDS
from agents.report_agent import (
    CHECK_INTERVAL,
    detect_surge,
    generate_regular_report,
    generate_surge_report,
    update_baseline,
)

# 防重入：重复调用 start_scheduled_tasks 只启动一个汇报线程
_started = False
_start_lock = threading.Lock()


def _publish(summary: str, data: dict):
    """入库 + WebSocket 广播 + 终端日志。"""
    try:
        import mysql_db
        mysql_db.save_heat_report(summary, data)
    except Exception as e:
        print(f"[Report] 入库失败: {e}")
    try:
        from api.routes.report import broadcast_report
        broadcast_report({
            "type": "report",
            "summary": summary,
            "data": data,
            "ts": datetime.now().isoformat(),
        })
    except Exception as e:
        print(f"[Report] 广播失败: {e}")
    print(f"[Report] {summary}")


def _run_heat_report():
    # 启动即生成一轮常规汇报，方便立即看到效果
    try:
        regular = generate_regular_report()
        _publish(regular["summary"], regular["data"])
    except Exception as e:
        print(f"[Report] 初始汇报失败: {e}")

    last_regular = time.time()
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            # 1) 异常突增检测（主动推送）
            surge = detect_surge()
            if surge:
                s = generate_surge_report(surge)
                _publish(s["summary"], s["data"])
        except Exception as e:
            print(f"[Report] 突增检测失败: {e}")

        # 2) 常规周期汇报
        now = time.time()
        if now - last_regular >= HEAT_REPORT_INTERVAL_SECONDS:
            last_regular = now
            try:
                regular = generate_regular_report()
                _publish(regular["summary"], regular["data"])
            except Exception as e:
                print(f"[Report] 常规汇报失败: {e}")

        # 3) 刷新告警基线
        try:
            from api.dependencies import get_anomaly_skill
            update_baseline(get_anomaly_skill().get_alert_summary())
        except Exception as e:
            print(f"[Report] 基线刷新失败: {e}")


def start_scheduled_tasks() -> threading.Thread | None:
    """启动后台定时任务线程（幂等：重复调用只启动一次）。"""
    global _started
    with _start_lock:
        if _started:
            print("[Report] 定时任务已在运行，跳过重复启动")
            return None
        _started = True
        thread = threading.Thread(target=_run_heat_report, daemon=True)
        thread.start()
        return thread
