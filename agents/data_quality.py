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

    `reliable=False` 表示「当前没有可信的实时数据」：此时业务统计里的 0
    应被理解为「无数据」，而不是「无客流/无告警」。

    ⚠ **两套口径，别用错**：

    | 字段 | 口径 | 适用 |
    |---|---|---|
    | `reliable`（顶层） | **任一路新鲜即为 True** | 只看"系统整体是否在收数据"，如监控/健康检查 |
    | `reliable_by_source[src]` | **该源自身**是否新鲜 | **业务结论必须用这个** |

    为什么要分：顶层口径会把"某一路停摆"掩盖掉。实测各源为
    `{'retail': 未新鲜, 'client': 新鲜}` 时顶层返回 `reliable=True`
    ——于是"浏览器在推帧"就把停摆的服务器摄像头粉饰成"数据可信"，
    系统照样输出"到访 0 人次""未发现异常"。
    业务侧请用 `is_reliable(SOURCE_RETAIL)` / `warning_text(SOURCE_RETAIL)`。
    """
    now = time.monotonic()
    with _lock:
        last = dict(_last_frame)
        counts = dict(_frame_count)

    sources = {}
    for src, ts in last.items():
        age = max(0.0, now - ts)
        fresh = age <= DATA_STALE_SECONDS
        sources[src] = {
            "label": _SOURCE_LABEL.get(src, src),
            "age_seconds": round(age, 1),
            "stale": not fresh,
            "reliable": fresh,          # 该源自身是否可信
            "frames": counts.get(src, 0),
        }

    reliable_by_source = {src: s["reliable"] for src, s in sources.items()}
    unreliable_sources = [src for src, ok in reliable_by_source.items() if not ok]

    # 从未收到任何帧 → 视频源未启动
    if not sources:
        return {
            "reliable": False,
            "reliable_by_source": {},
            "unreliable_sources": [],
            "stale_seconds": DATA_STALE_SECONDS,
            "reason": "视频源未启动，当前没有任何视频帧，统计值 0 不代表真实客流",
            "sources": {},
        }

    if any(reliable_by_source.values()):
        return {
            "reliable": True,           # 聚合口径（见 docstring，业务侧别直接用）
            "reliable_by_source": reliable_by_source,
            "unreliable_sources": unreliable_sources,
            "stale_seconds": DATA_STALE_SECONDS,
            "reason": "",
            "sources": sources,
        }

    oldest_active = min(s["age_seconds"] for s in sources.values())
    return {
        "reliable": False,
        "reliable_by_source": reliable_by_source,
        "unreliable_sources": unreliable_sources,
        "stale_seconds": DATA_STALE_SECONDS,
        "reason": (
            f"视频源已断流约 {int(oldest_active)} 秒"
            f"（阈值 {DATA_STALE_SECONDS} 秒），当前数据不可信"
        ),
        "sources": sources,
    }


def is_reliable(source: str | None = None) -> bool:
    """数据是否可信。

    - 指定 `source`：按**该源**判定 —— 业务侧应当这样用（见 `snapshot` 的说明）
    - 不指定：按"任一路新鲜"的聚合口径（向后兼容，仅适合整体健康判断）
    """
    snap = snapshot()
    if source is None:
        return bool(snap["reliable"])
    return bool(snap.get("reliable_by_source", {}).get(source, False))


def warning_text(source: str | None = None) -> str:
    """给报表/问答用的一句话告警；数据可信时返回空串。

    指定 `source` 时按该源判定，并在"其它源仍在推送"时**点明是哪一路缺数据**
    ——否则读者会以为整体正常。
    """
    snap = snapshot()

    if source is None:
        if snap["reliable"]:
            return ""
        return f"【数据不可信】{snap['reason']}。"

    if snap.get("reliable_by_source", {}).get(source, False):
        return ""

    label = _SOURCE_LABEL.get(source, source)
    if snap.get("reliable"):
        # 其它源有数据，但业务结论所依赖的这一路没有 —— 必须点名
        others = [s for s in snap.get("reliable_by_source", {}) if s != source]
        others_txt = "、".join(_SOURCE_LABEL.get(o, o) for o in others) or "其它来源"
        return (f"【数据不可信】{label}当前没有视频帧"
                f"（注意：{others_txt}仍在推送，但本次结论依赖的是{label}）。"
                f"统计值 0 不代表真实客流/无异常。")
    return f"【数据不可信】{snap['reason']}。"
