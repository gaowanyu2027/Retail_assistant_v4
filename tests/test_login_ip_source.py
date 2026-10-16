"""登录来源 IP 口径：X-Forwarded-For 采信开关 + 登录限流的限流键。

背景（这条为什么必须盯住）：Go edge 作为反向代理上线后，Python 侧看到的
对端**全部等于 edge 容器 IP**。若登录失败限流的键仍取直连对端，跨账号累计
达到 IP 维度阈值（AUTH_MAX_FAILED_ATTEMPTS × AUTH_IP_ATTEMPT_MULTIPLIER，默认
5×5=25）后，`ip:{edge_ip}` 会被锁定 —— 表现为**所有用户一起登不进来**。

修法：限流的键与审计展示共用 `client_ip_from_request`；它只在
`AUTH_TRUST_FORWARDED_FOR=1` 时采信 XFF 第一跳，而该开关只有在入口代理会
**剥离入站 XFF 并重设**时才可以打开（Go edge 即如此，见 edge/internal/proxy）。

本文件用**临时 SQLite**（复用 test_auth 的建库手法），不碰线上 auth.db。
"""
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from api import security as S  # noqa: E402
from api.routes import auth as auth_routes  # noqa: E402
from config.settings import (  # noqa: E402
    AUTH_IP_ATTEMPT_MULTIPLIER,
    AUTH_MAX_FAILED_ATTEMPTS,
)
from test_auth import _cleanup, _fresh_db  # noqa: E402

PROXY_PEER = ("172.18.0.5", 40000)   # 模拟"对端永远是 edge 容器"的直连地址


def _request(peer, xff=None):
    """构造最小 Starlette Request（只用到 client 与 headers）。"""
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/api/auth/login", "raw_path": b"/api/auth/login",
        "query_string": b"", "headers": headers,
        "client": peer, "server": ("testserver", 80),
    })


class _flag:
    """临时改动 AUTH_TRUST_FORWARDED_FOR（自研运行器无 fixture，自行复位）。"""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = S.AUTH_TRUST_FORWARDED_FOR
        S.AUTH_TRUST_FORWARDED_FOR = self.value

    def __exit__(self, *exc):
        S.AUTH_TRUST_FORWARDED_FOR = self.old


def _login(peer, xff, username="u", password="p"):
    """直接驱动路由协程，返回状态码（成功走不到，失败即 HTTPException）。"""
    req = _request(peer, xff)
    body = auth_routes.LoginRequest(username=username, password=password)
    try:
        asyncio.run(auth_routes.login(body, Response(), req))
        return 200
    except HTTPException as e:
        return e.status_code


# ---------------- 来源 IP 取值本身 ----------------

def test_flag_off_ignores_xff():
    """默认（开关关闭）：XFF 是客户端可伪造的展示字段，不影响取值。"""
    with _flag(False):
        assert S.client_ip_from_request(
            _request(("203.0.113.9", 1234), "6.6.6.6, 7.7.7.7")
        ) == "203.0.113.9"
        assert S.client_ip_from_request(_request(None, "6.6.6.6")) == ""
        assert S.client_ip_from_request(None) == ""


def test_flag_on_uses_first_forwarded_hop():
    """开关打开（入口代理已剥离并重设 XFF）：取第一跳，空值回退直连对端。"""
    with _flag(True):
        assert S.client_ip_from_request(
            _request(PROXY_PEER, " 6.6.6.6 , 7.7.7.7")
        ) == "6.6.6.6"
        assert S.client_ip_from_request(_request(PROXY_PEER, "")) == "172.18.0.5"
        assert S.client_ip_from_request(_request(PROXY_PEER, None)) == "172.18.0.5"


# ---------------- 路由把哪个 IP 交给限流 ----------------

def test_login_route_passes_trusted_ip_to_throttle():
    seen = []

    def fake_throttle(username, password, ip=""):
        seen.append(ip)
        return {"ok": False, "reason": "bad_credentials"}

    old = auth_routes.login_with_throttle
    auth_routes.login_with_throttle = fake_throttle
    try:
        with _flag(True):
            assert _login(PROXY_PEER, "6.6.6.6, 7.7.7.7") == 401
        with _flag(False):
            assert _login(PROXY_PEER, "6.6.6.6, 7.7.7.7") == 401
    finally:
        auth_routes.login_with_throttle = old

    assert seen == ["6.6.6.6", "172.18.0.5"], f"限流键取错: {seen}"


# ---------------- 端到端：反代拓扑不锁死全体 ----------------

def test_proxy_topology_does_not_globally_lock_users():
    """25 次来自同一真实来源的失败只锁该来源；其它来源照常可登录。

    这条不成立 = "edge 上线后全员登录被锁死"（迁移红线）。
    """
    _fresh_db()
    old_auth = S.authenticate
    S.authenticate = lambda username, password: None   # 一律失败，只测限流维度
    try:
        ip_limit = AUTH_MAX_FAILED_ATTEMPTS * AUTH_IP_ATTEMPT_MULTIPLIER
        per_user = AUTH_MAX_FAILED_ATTEMPTS
        with _flag(True):
            # 轮换用户名（避免先触发"用户名+IP"维度），凑满 IP 维度阈值
            for i in range(ip_limit):
                _login(PROXY_PEER, "1.1.1.1", username=f"attacker{i // per_user}")
            # ① 触发来源被锁（限流本身仍然有效）
            assert S.check_login_allowed("someone", "1.1.1.1")[0] is False
            assert _login(PROXY_PEER, "1.1.1.1", username="someone") == 429
            # ② 其它来源不受影响 ← 红线所在
            assert S.check_login_allowed("someone", "2.2.2.2")[0] is True
            assert _login(PROXY_PEER, "2.2.2.2", username="realuser") == 401
    finally:
        S.authenticate = old_auth
        _cleanup()
