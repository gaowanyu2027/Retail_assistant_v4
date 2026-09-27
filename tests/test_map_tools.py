"""百度地图工具（`agents/map_tools.py`）的离线单测。

覆盖三件**实测踩过**的事（都不需要网络：`httpx.get` 被替换成假实现）：

1. **端点**：距离测算必须打 `/routematrix/v2/driving`。
   旧路径 `/distancematrix/v1/driving` 会 302 到百度错误页（线上实测），
   这里用回归断言把它钉死。
2. **非 200 / 非 JSON**：百度对"服务路径或权限不匹配"会返回 302 + 空 body，
   旧实现直接 `resp.json()` 只会得到一句 `Expecting value: line 1 column 1`，
   对使用者毫无信息量 → 现在必须给出可读错误。
3. **降级**：算路服务不可用（配额/权限/网络）时退化为**直线距离**，
   并带 `mode` 与 `degrade_reason` 标注，避免把直线当驾车用。

运行：`python tests/run_tests.py`（自研运行器不调用 setup/fixture，用例自己复位状态）。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import agents.map_tools as m


class _Resp:
    """最小化的 httpx 响应替身。"""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload, ensure_ascii=False) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _FakeHttpx:
    """替换 `agents.map_tools.httpx`：记录调用、返回预设响应。"""

    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return self._resp


def _patch(resp):
    fake = _FakeHttpx(resp)
    old = m.httpx
    m.httpx = fake
    m._MAP_CACHE.clear()
    return fake, old


def _unpatch(old):
    m.httpx = old
    m._MAP_CACHE.clear()


def test_request_rejects_non_200_with_readable_message():
    """302 + 空 body（百度错误页）→ 可读错误，而不是 'Expecting value …'。"""
    fake, old = _patch(_Resp(status_code=302))
    try:
        m.BAIDU_MAP_AK, saved_ak = "fake-ak", m.BAIDU_MAP_AK
        out = m._request("/whatever", {"a": 1})
        assert out["status"] == "error", out
        assert "302" in out["message"], out
        assert "Expecting value" not in out["message"], out
    finally:
        m.BAIDU_MAP_AK = saved_ak
        _unpatch(old)


def test_request_rejects_non_json_body():
    """HTTP 200 但不是 JSON（错误页/网关拦截）→ 也要给出可读错误。"""
    fake, old = _patch(_Resp(status_code=200, payload=None, text="<html>error</html>"))
    try:
        saved_ak = m.BAIDU_MAP_AK
        m.BAIDU_MAP_AK = "fake-ak"
        out = m._request("/whatever", {"a": 1})
        assert out["status"] == "error", out
        assert "非 JSON" in out["message"], out
    finally:
        m.BAIDU_MAP_AK = saved_ak
        _unpatch(old)


def test_request_missing_ak_returns_readable_error():
    """未配置 AK：返回可读错误（fail-open），不抛异常。"""
    saved_ak = m.BAIDU_MAP_AK
    m.BAIDU_MAP_AK = ""
    try:
        out = m._request("/whatever", {})
        assert out["status"] == "error" and "未配置百度地图 AK" in out["message"], out
    finally:
        m.BAIDU_MAP_AK = saved_ak


def test_calc_distances_uses_routematrix_and_parses_flat_result():
    """回归：必须走 /routematrix/v2/driving，并解析平铺 result（按终点顺序）。"""
    payload = {
        "status": 0, "message": "成功", "num": 2,
        "result": [
            {"distance": {"text": "7.2公里", "value": 7222}, "duration": {"text": "20分钟", "value": 1217}},
            {"distance": {"text": "2.1公里", "value": 2111}, "duration": {"text": "7分钟", "value": 408}},
        ],
    }
    fake, old = _patch(_Resp(200, payload))
    try:
        saved_ak, m.BAIDU_MAP_AK = m.BAIDU_MAP_AK, "fake-ak"
        out = json.loads(m.calc_distances(116.404, 39.915, [{"lng": 116.46, "lat": 39.91}, {"lng": 116.397, "lat": 39.908}]))
        # ① 端点正确（旧路径会 302）
        assert fake.calls and fake.calls[0]["url"].endswith("/routematrix/v2/driving"), fake.calls
        # ② 参数顺序：百度要 lat,lng
        assert fake.calls[0]["params"]["origins"] == "39.915,116.404", fake.calls[0]["params"]
        assert fake.calls[0]["params"]["destinations"] == "39.91,116.46|39.908,116.397", fake.calls[0]["params"]
        # ③ 平铺解析 + 顺序不乱
        assert [r["distance_m"] for r in out["routes"]] == [7222, 2111], out
        assert out["routes"][0]["duration_text"] == "20分钟", out
        assert out["mode"] == "driving" and out["total_distance_m"] == 9333, out
    finally:
        m.BAIDU_MAP_AK = saved_ak
        _unpatch(old)


def test_calc_distances_degrades_to_straight_line_when_service_unavailable():
    """算路不可用（302）→ 降级为直线距离，并明确标注 mode 与 degrade_reason。"""
    fake, old = _patch(_Resp(status_code=302))
    try:
        saved_ak, m.BAIDU_MAP_AK = m.BAIDU_MAP_AK, "fake-ak"
        dest = {"lng": 116.46, "lat": 39.91}
        out = json.loads(m.calc_distances(116.404, 39.915, [dest]))
        assert out["mode"] == "straight_line", out
        assert "302" in out.get("degrade_reason", ""), out
        expect = m._haversine(116.404, 39.915, dest["lng"], dest["lat"])
        assert abs(out["routes"][0]["distance_m"] - round(expect, 1)) < 1.0, out
        assert "直线" in out["routes"][0]["distance_text"], out
        assert out["routes"][0]["duration_s"] is None, out
    finally:
        m.BAIDU_MAP_AK = saved_ak
        _unpatch(old)


def test_calc_distances_rejects_empty_dests():
    out = json.loads(m.calc_distances(116.404, 39.915, []))
    assert out["status"] == "error" and "dests 不能为空" in out["message"], out


def test_calc_distances_hits_cache_on_second_call():
    """同一请求第二次命中 TTL 缓存：不再打百度（省配额）。"""
    payload = {"status": 0, "result": [
        {"distance": {"text": "1公里", "value": 1000}, "duration": {"text": "3分钟", "value": 180}}]}
    fake, old = _patch(_Resp(200, payload))
    try:
        saved_ak, m.BAIDU_MAP_AK = m.BAIDU_MAP_AK, "fake-ak"
        args = (116.404, 39.915, [{"lng": 116.46, "lat": 39.91}])
        m.calc_distances(*args)
        m.calc_distances(*args)
        assert len(fake.calls) == 1, f"应只回源一次，实际 {len(fake.calls)} 次"
    finally:
        m.BAIDU_MAP_AK = saved_ak
        _unpatch(old)
