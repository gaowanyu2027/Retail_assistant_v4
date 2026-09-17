"""凭据优先级测试（台账 B13）。

被修的错误：原实现是"Cookie 命中就直接返回，Bearer 根本不看" —— 浏览器里一个
**别的应用/旧会话留下的垃圾 Cookie**，会让同一请求里**有效的** Bearer 令牌失效（401）。
小程序、脚本、以及"登录页刚清过 cookie"的场景都会中招。

修法：返回候选凭据列表（显式 `Authorization` 优先、隐式 Cookie 兜底），
由调用方**逐个尝试解析**，任一有效即通过。

测试直接构造 Starlette Request / ASGI scope；会话查找用假函数替换，离线可跑。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from starlette.requests import Request  # noqa: E402

from api import security as S  # noqa: E402

GOOD = "good-token"


def _cookie(value: str) -> bytes:
    """按真实 Cookie 名构造（不要写死名字，改名了要能自动跟上）。"""
    return f"{S.COOKIE_NAME}={value}".encode()


def _fake_sessions(valid=(GOOD,)):
    """把会话查找换成假函数：只有 valid 里的令牌能解析出用户。"""
    return lambda tok: ({"username": "u", "role": "admin"} if tok in valid else None)


def _http_scope(headers):
    return {"type": "http", "method": "GET", "path": "/api/x",
            "headers": headers, "query_string": b""}


def _req(headers):
    return Request(_http_scope(headers))


def _bearer(tok: str) -> bytes:
    return b"authorization", ("Bearer " + tok).encode()


# ---------------- 候选顺序 ----------------

def test_explicit_bearer_comes_first():
    cands = S.token_candidates_from_request(
        _req([(b"cookie", _cookie("garbage")), _bearer(GOOD)])
    )
    assert [s for s, _ in cands] == ["bearer", "cookie"], cands
    assert cands[0][1] == GOOD


def test_cookie_used_when_no_bearer():
    cands = S.token_candidates_from_request(_req([(b"cookie", _cookie("abc"))]))
    assert cands == [("cookie", "abc")], cands


def test_no_credentials_gives_empty_list():
    assert S.token_candidates_from_request(_req([])) == []


def test_blank_values_are_ignored():
    """`Authorization: Bearer `（空）与 `retail_sid=`（空）都不算凭据。"""
    cands = S.token_candidates_from_request(
        _req([(b"authorization", b"Bearer   "), (b"cookie", _cookie(""))])
    )
    assert cands == [], cands


# ---------------- 核心回归：垃圾 Cookie 不能挡住有效 Bearer ----------------

def test_garbage_cookie_does_not_break_valid_bearer_http():
    old = S.get_session
    S.get_session = _fake_sessions()
    try:
        req = _req([(b"cookie", _cookie("stale-junk")), _bearer(GOOD)])
        assert S.user_from_request(req) == {"username": "u", "role": "admin"}
    finally:
        S.get_session = old


def test_garbage_cookie_does_not_break_valid_bearer_ws():
    """WebSocket 握手走的是 scope 那条路径，同样要修（小程序/插件都靠它）。"""
    old = S.get_session
    S.get_session = _fake_sessions()
    try:
        scope = _http_scope([(b"cookie", _cookie("stale-junk")), _bearer(GOOD)])
        assert S.user_from_scope(scope) == {"username": "u", "role": "admin"}
        assert [s for s, _ in S.token_candidates_from_scope(scope)] == ["bearer", "cookie"]
    finally:
        S.get_session = old


def test_cookie_still_works_when_bearer_is_garbage():
    """反向也要成立：Bearer 是垃圾、Cookie 有效时不能反而挂掉（浏览器正常路径）。"""
    old = S.get_session
    S.get_session = _fake_sessions()
    try:
        req = _req([(b"cookie", _cookie(GOOD)), _bearer("junk")])
        assert S.user_from_request(req) is not None
    finally:
        S.get_session = old


def test_both_garbage_gives_no_user():
    old = S.get_session
    S.get_session = _fake_sessions()
    try:
        req = _req([(b"cookie", _cookie("junk")), _bearer("junk2")])
        assert S.user_from_request(req) is None
    finally:
        S.get_session = old


def test_token_from_request_keeps_old_contract():
    """旧接口仍返回"优先级最高的那个令牌"（其它调用方依赖这个契约）。"""
    req = _req([(b"cookie", _cookie("abc")), _bearer(GOOD)])
    assert S.token_from_request(req) == GOOD
    assert S.token_from_request(_req([(b"cookie", _cookie("abc"))])) == "abc"
    assert S.token_from_request(_req([])) is None
