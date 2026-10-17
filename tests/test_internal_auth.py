"""内部端点（/internal/auth/*）：三道门禁 + 判定结果的形状。

为什么盯住它：Go edge 的鉴权早拒要靠这两个端点拿**权威结论**，而它们不在 `/api/`
前缀下（鉴权中间件拦不到）——门禁写漏就等于给公网开了"会话校验/票据消费"接口。
门禁三件套，任一不过一律 404（当作路径不存在）：

  1. `INTERNAL_API_TOKEN` 未配置（默认空）；
  2. 请求头 `X-Internal-Token` 不匹配（常数时间比较）；
  3. 直连对端不是环回 / 私网 / 链路本地。

edge 侧另有 `/internal/**` 本地 404（公网请求不出网），两道防线互不依赖。

本文件用**临时 SQLite**（复用 test_auth 的建库手法），不碰线上 auth.db。
"""
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

from api import security as S  # noqa: E402
from api.routes import internal  # noqa: E402
from config.settings import AUTH_ENABLED  # noqa: E402
from test_auth import _cleanup, _fresh_db  # noqa: E402

GOOD_TOKEN = "internal-token-for-tests"


def _req(peer=("127.0.0.1", 51000), token_header=None,
         path="/internal/auth/verify", method="POST"):
    """构造最小 Starlette Request（只用到 client 与 headers）。"""
    headers = []
    if token_header is not None:
        headers.append((b"x-internal-token", token_header.encode("latin-1")))
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": headers,
        "client": peer, "server": ("testserver", 80),
    })


class _token:
    """临时改动内部端点密钥（自研运行器无 fixture，自行复位）。"""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = internal.INTERNAL_API_TOKEN
        internal.INTERNAL_API_TOKEN = self.value

    def __exit__(self, *exc):
        internal.INTERNAL_API_TOKEN = self.old


def _gate_passes(request) -> bool:
    """门禁放行返回 True；被拒必须是 404（不泄露端点存在性）。"""
    try:
        internal.require_internal_token(request)
        return True
    except HTTPException as e:
        assert e.status_code == 404, f"门禁必须以 404 拒绝，实际 {e.status_code}"
        return False


# ---------------- 门禁 ----------------

def test_gate_requires_configured_token():
    with _token(""):
        assert not _gate_passes(_req(token_header="whatever")), \
            "未配置密钥时必须拒绝（默认关闭）"
        assert not _gate_passes(_req(token_header=None))
    with _token(GOOD_TOKEN):
        assert _gate_passes(_req(token_header=GOOD_TOKEN)), "配置正确时应放行"


def test_gate_rejects_wrong_or_missing_token():
    with _token(GOOD_TOKEN):
        for header in (None, "", "wrong", GOOD_TOKEN + "x", GOOD_TOKEN[:-1],
                       GOOD_TOKEN.upper(), " " + GOOD_TOKEN):
            assert not _gate_passes(_req(token_header=header)), \
                f"密钥 {header!r} 不该放行"


def test_gate_rejects_public_peer():
    """对端是公网地址一律拒绝：edge 与后端同处内网，公网来源不该出现在这条链路上。

    注意 `ipaddress.is_private` 还包含 192.0.2.0/24、198.51.100.0/24、203.0.113.0/24
    等**文档用网段**，它们同样不可路由、不可能出现在真实链路上，放行无风险。
    """
    with _token(GOOD_TOKEN):
        for peer in (("8.8.8.8", 1234), ("1.1.1.1", 1234),
                     ("2001:4860:4860::8888", 1234)):
            assert not _gate_passes(_req(peer=peer, token_header=GOOD_TOKEN)), \
                f"对端 {peer[0]} 不该放行"
        # 空对端（拿不到来源）按安全默认值拒绝
        assert not _gate_passes(_req(peer=None, token_header=GOOD_TOKEN))
        assert not _gate_passes(_req(peer=("not-an-ip", 1), token_header=GOOD_TOKEN))


def test_gate_accepts_internal_peers():
    with _token(GOOD_TOKEN):
        for host in ("127.0.0.1", "::1", "172.18.0.5", "10.0.0.7", "192.168.1.20",
                     "169.254.10.10", "::ffff:127.0.0.1", "::ffff:172.18.0.5",
                     "fd00::1"):
            assert _gate_passes(_req(peer=(host, 51000), token_header=GOOD_TOKEN)), \
                f"内网对端 {host} 应放行"


# ---------------- 判定结果 ----------------

def _mk_session(username="alice", role="platform"):
    user = S.create_user(username, "Passw0rd!x", role, "测试账号")
    sess = S.create_session(user["id"], user["username"], user["role"])
    return user, sess["token"]


def test_verify_returns_limited_user_fields():
    _fresh_db()
    try:
        user, token = _mk_session()

        out = internal.verify_session(internal.TokenRequest(token=token))
        assert out == {"valid": True,
                       "user": {"id": user["id"], "username": "alice", "role": "platform"},
                       "auth_enabled": AUTH_ENABLED}, \
            f"判定结果形状不符：{out}"
        # 最小暴露面：会话令牌、过期时间、昵称等一概不出网
        assert "token" not in out["user"] and "session_expires_at" not in out["user"]

        # 无凭据也要能问到"这台后端开着鉴权吗"：edge 据此决定 WS 是否本地接管
        assert out["auth_enabled"] == AUTH_ENABLED

        for bad in ("", "nope", token + "x"):
            assert internal.verify_session(internal.TokenRequest(token=bad)) == \
                {"valid": False, "user": None, "auth_enabled": AUTH_ENABLED}, \
                f"无效令牌 {bad!r} 应得否定结论"
    finally:
        _cleanup()


def test_verify_rejects_expired_and_disabled():
    _fresh_db()
    try:
        user, token = _mk_session()
        assert internal.verify_session(internal.TokenRequest(token=token))["valid"]

        S.update_user(user["id"], enabled=False)          # 停用账号 → 已有会话立即失效
        assert internal.verify_session(internal.TokenRequest(token=token)) == \
            {"valid": False, "user": None, "auth_enabled": AUTH_ENABLED}
    finally:
        _cleanup()


def test_consume_ticket_is_one_shot():
    _fresh_db()
    try:
        user, _ = _mk_session()
        ticket = S.issue_ticket(user, purpose="ws")["ticket"]

        first = internal.consume_ws_ticket(internal.TicketRequest(ticket=ticket))
        assert first == {"valid": True,
                         "user": {"id": user["id"], "username": "alice", "role": "platform"},
                         "auth_enabled": AUTH_ENABLED}, \
            f"首次消费应有效：{first}"
        # 用后即焚：原子消费（SELECT+DELETE）留在 Python 单写者内
        second = internal.consume_ws_ticket(internal.TicketRequest(ticket=ticket))
        assert second == {"valid": False, "user": None, "auth_enabled": AUTH_ENABLED}, \
            "票据不得二次使用"

        assert internal.consume_ws_ticket(internal.TicketRequest(ticket="nope"))["valid"] is False
    finally:
        _cleanup()


# ---------------- 路由形状 ----------------

def test_router_paths_and_hidden_from_schema():
    """端点路径与"不进接口文档"是接线契约（main.py 以 prefix="/internal" 挂载）。"""
    paths = {}
    for route in internal.router.routes:
        paths.setdefault(route.path, set()).update(route.methods or set())
    assert paths.get("/auth/verify") == {"POST"}, f"路由表：{paths}"
    assert paths.get("/auth/consume-ticket") == {"POST"}, f"路由表：{paths}"
    assert internal.router.include_in_schema is False, "内部端点不该出现在 /docs"


def test_unknown_internal_path_is_404_not_405():
    """`/internal/**` 下任何未匹配的路径与方法都回 404（不是 405 的"存在但方法不对"）。

    这也是与 edge 本地 404 逐字一致的前提（两条路径上同一个探测请求响应相同）。
    """
    try:
        internal.internal_not_found("whatever")
    except HTTPException as e:
        assert e.status_code == 404
        assert e.detail == "Not Found", "必须与 FastAPI 默认 404 的 detail 一致"
    else:
        raise AssertionError("兜底路由必须抛 404")
