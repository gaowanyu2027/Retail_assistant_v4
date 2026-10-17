"""主动汇报广播：EDGE_REPORT=1 时把消息交给 Go edge（S4）。

迁移前，汇报 WS 的订阅集合与事件循环都是 Python 的模块级单例
（`_report_clients` / `_report_loop`）；S4 把连接与扇出交给 edge 后，
Python 只负责"生成消息 + 投递给 edge"。

本文件只测**投递这一段**，不发真实网络（桩 opener 记录请求）：
  1. EDGE_REPORT=1 且密钥/地址齐备 → POST 到内网端点，JSON 与密钥正确；
  2. 开关关闭 / 地址为空 / 密钥为空 → 一条都不发；
  3. edge 不可达 → 只记日志、不抛出（汇报不能拖垮定时任务）；
  4. `broadcast_report` 同时走本地与 edge 两条路（回退拓扑仍可用）。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.routes import report as report_mod  # noqa: E402


class _Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """桩 opener：记录每次请求；可配置为直接抛错。"""

    def __init__(self, status=200, boom=False):
        self.status = status
        self.boom = boom
        self.requests = []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        if self.boom:
            raise OSError("edge down")
        return _Resp(self.status)


def _patch(enabled=True, url="http://edge:8000", token="tok", opener=None):
    """临时改写模块级配置，返回 restore()。调用方必须放在 finally 里。"""
    old = (report_mod.EDGE_REPORT, report_mod.EDGE_REPORT_URL,
           report_mod.INTERNAL_API_TOKEN, report_mod._EDGE_OPENER)
    report_mod.EDGE_REPORT = enabled
    report_mod.EDGE_REPORT_URL = url
    report_mod.INTERNAL_API_TOKEN = token
    report_mod._EDGE_OPENER = opener or _Opener()

    def restore():
        (report_mod.EDGE_REPORT, report_mod.EDGE_REPORT_URL,
         report_mod.INTERNAL_API_TOKEN, report_mod._EDGE_OPENER) = old

    return restore


def test_edge_broadcast_posts_json_with_internal_token():
    opener = _Opener()
    restore = _patch(opener=opener)
    try:
        payload = {"type": "report", "summary": "1号货架热度上升", "data": {"zone": "A"}}
        assert report_mod._broadcast_edge(payload) is True
        assert len(opener.requests) == 1, opener.requests
        req, timeout = opener.requests[0]
        assert req.full_url == "http://edge:8000/internal/report/broadcast", req.full_url
        assert req.get_method() == "POST", req.get_method()
        assert req.get_header("X-internal-token") == "tok", req.headers
        # ensure_ascii=False：中文原样发给 edge，避免无谓的体积膨胀
        body = json.loads(req.data.decode("utf-8"))
        assert body == payload, body
        assert "1号货架" in req.data.decode("utf-8")
        assert timeout == 3
    finally:
        restore()


def test_edge_broadcast_is_noop_when_disabled_or_incomplete():
    for kwargs in (
        {"enabled": False},
        {"url": ""},
        {"token": ""},
    ):
        opener = _Opener()
        restore = _patch(opener=opener, **kwargs)
        try:
            assert report_mod._broadcast_edge({"type": "report"}) is False, kwargs
            assert opener.requests == [], (kwargs, opener.requests)
        finally:
            restore()


def test_edge_broadcast_failure_never_raises():
    restore = _patch(opener=_Opener(boom=True))
    try:
        assert report_mod._broadcast_edge({"type": "report"}) is False
    finally:
        restore()


def test_non_2xx_is_reported_as_failure():
    restore = _patch(opener=_Opener(status=404))
    try:
        assert report_mod._broadcast_edge({"type": "report"}) is False
    finally:
        restore()


def test_broadcast_report_delivers_locally_and_to_edge():
    calls = []
    old_local, old_edge = report_mod._broadcast_local, report_mod._broadcast_edge
    report_mod._broadcast_local = lambda p: calls.append(("local", p))
    report_mod._broadcast_edge = lambda p: calls.append(("edge", p))
    try:
        payload = {"type": "report", "summary": "x"}
        report_mod.broadcast_report(payload)
    finally:
        report_mod._broadcast_local = old_local
        report_mod._broadcast_edge = old_edge
    assert calls == [("local", payload), ("edge", payload)], calls
