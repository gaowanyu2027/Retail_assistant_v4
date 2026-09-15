"""
数据可信度门禁 — 区分「真的没生意」与「设备/数据异常」

问题背景（实测复现）：
    摄像头未启动 / 断流 / 离线时，各 skill 的统计值仍然是 0。
    若把 0 直接当业务事实输出，店长会误判为「今天没客流」，
    LLM 也会基于假数据给出运营归因——实测在没有任何视频源时，
    主动汇报照样生成了「各货架到访 0 人次，热度 0.0」这类结论。

做法：
    1. 各帧处理入口调用 mark_frame(source) 上报「最后一次成功出帧」时刻
    2. 评估时按 DATA_STALE_SECONDS 判定是否过期
    3. snapshot() 供报表 / 问答 / 主动汇报标注「数据不可信」并给出原因

原则：宁可说「没有数据」，也不把设备故障输出成业务结论。
"""
import threading
import time

from config.settings import DATA_STALE_SECONDS

# 数据源标识
SOURCE_RETAIL = "retail"     # 服务器摄像头 - 零售分析
SOURCE_EMOTION = "emotion"   # 服务器摄像头 - 表情分析
SOURCE_CLIENT = "client"     # 本机（浏览器/小程序）推帧

_SOURCE_LABEL = {
    SOURCE_RETAIL: "服务器摄像头（零售分析）",
    SOURCE_EMOTION: "服务器摄像头（表情分析）",
    SOURCE_CLIENT: "本机摄像头（客户端推帧）",
}

_lock = threading.Lock()
_last_frame: dict[str, float] = {}   # source -> 最后一次出帧的单调时钟
_frame_count: dict[str, int] = {}    # source -> 累计帧数


def mark_frame(source: str) -> None:
    """上报一帧已成功处理（由视频处理线程 / 客户端推帧通道调用）。"""
    now = time.monotonic()
    with _lock:
        _last_frame[source] = now
        _frame_count[source] = _frame_count.get(source, 0) + 1


def reset() -> None:
    """清空状态（切换视频源 / 测试时使用）。"""
    with _lock:
        _last_frame.clear()
        _frame_count.clear()


def snapshot() -> dict:
    """返回数据可信度快照。

    reliable=False 表示「当前没有可信的实时数据」：此时业务统计里的 0
    应被理解为「无数据」，而不是「无客流/无告警」。
    """
    now = time.monotonic()
    with _lock:
        last = dict(_last_frame)
        counts = dict(_frame_count)

    sources = {}
    for src, ts in last.items():
        age = max(0.0, now - ts)
        sources[src] = {
            "label": _SOURCE_LABEL.get(src, src),
            "age_seconds": round(age, 1),
            "stale": age > DATA_STALE_SECONDS,
            "frames": counts.get(src, 0),
        }

    # 从未收到任何帧 → 视频源未启动
    if not sources:
        return {
            "reliable": False,
            "stale_seconds": DATA_STALE_SECONDS,
            "reason": "视频源未启动，当前没有任何视频帧，统计值 0 不代表真实客流",
            "sources": {},
        }

    # 任一路新鲜 → 数据可信（多路场景下不因某一路空闲而整体判死）
    if any(not s["stale"] for s in sources.values()):
        return {
            "reliable": True,
            "stale_seconds": DATA_STALE_SECONDS,
            "reason": "",
            "sources": sources,
        }

    oldest_active = min(s["age_seconds"] for s in sources.values())
    return {
        "reliable": False,
        "stale_seconds": DATA_STALE_SECONDS,
        "reason": (
            f"视频源已断流约 {int(oldest_active)} 秒"
            f"（阈值 {DATA_STALE_SECONDS} 秒），当前数据不可信"
        ),
        "sources": sources,
    }


def is_reliable() -> bool:
    """当前是否存在可信的实时数据。"""
    return snapshot()["reliable"]


def warning_text() -> str:
    """给报表/问答用的一句话告警；数据可信时返回空串。"""
    snap = snapshot()
    if snap["reliable"]:
        return ""
    return f"【数据不可信】{snap['reason']}。"
