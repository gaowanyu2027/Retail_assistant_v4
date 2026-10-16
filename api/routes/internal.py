"""
内部端点 — 仅供内网同伴进程调用（Go edge），**不面向公网**。

为什么存在：边缘网关要在转发之前判断"这个凭据还有效吗"，但又不能自己持有
`data/auth.db`（铁律③：一表一个写者、安全关键代码不复制）。于是把判定做成
内部端点，会话库仍然只有 Python 一个写者：

- POST /internal/auth/verify          校验会话令牌（复用 get_session，含滑动续期）
- POST /internal/auth/consume-ticket  一次性票据的**原子消费**（SELECT+DELETE 留在这里）

调用方（edge）自己只做几秒的正/负缓存，权威判定永远是这两个端点。

三道门禁，任一不过一律 **404**（当作路径不存在，不泄露端点是否存在）：
1. `INTERNAL_API_TOKEN` 未配置（默认空）→ 404；
2. 请求头 `X-Internal-Token` 与配置值不相等（常数时间比较）→ 404；
3. 直连对端不是环回 / 私网 / 链路本地地址 → 404。
   edge 与后端同处一个容器网络，公网来源压根不该出现在这条链路上。

edge 侧另有 `/internal/**` 本地 404（不出网），两道防线互不依赖。

这些路由用 `def`（非 async）：底层是进程内单 SQLite 连接 + 全局锁，
跑在 FastAPI 线程池里不会占住事件循环（本文件的调用频率由 edge 缓存压制）。
"""
import ipaddress
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.security import consume_ticket, get_session
from config.settings import INTERNAL_API_TOKEN

router = APIRouter(tags=["internal"], include_in_schema=False)


# ==================== 请求模型 ====================

class TokenRequest(BaseModel):
    token: str = ""


class TicketRequest(BaseModel):
    ticket: str = ""


# ==================== 门禁 ====================

def _peer_host(request: Request) -> str:
    client = request.client
    return (client.host if client else "") or ""


def _is_trusted_peer(host: str) -> bool:
    """对端是否为内网地址（环回 / 私网 / 链路本地；IPv4-mapped IPv6 先解包）。"""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def require_internal_token(request: Request) -> None:
    """FastAPI 依赖：不满足即 404。"""
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=404)
    provided = request.headers.get("x-internal-token") or ""
    if not secrets.compare_digest(provided.encode("utf-8"),
                                  INTERNAL_API_TOKEN.encode("utf-8")):
        raise HTTPException(status_code=404)
    if not _is_trusted_peer(_peer_host(request)):
        raise HTTPException(status_code=404)


# ==================== 响应构造 ====================

def _verdict(user: dict | None) -> dict:
    """统一的判定结果：只回 edge 用得上的三个字段（最小暴露面）。

    会话令牌 / 过期时间 / 昵称等一概不出网 —— edge 只需要"有效吗、是谁、什么角色"。
    """
    if not user:
        return {"valid": False, "user": None}
    return {
        "valid": True,
        "user": {
            "id": user.get("id"),
            "username": user.get("username"),
            "role": user.get("role"),
        },
    }


# ==================== 端点 ====================

@router.post("/auth/verify", dependencies=[Depends(require_internal_token)])
def verify_session(payload: TokenRequest) -> dict:
    """校验会话令牌；有效则顺带刷新 last_seen（本就有 60s 写限流）。"""
    return _verdict(get_session(payload.token))


@router.post("/auth/consume-ticket", dependencies=[Depends(require_internal_token)])
def consume_ws_ticket(payload: TicketRequest) -> dict:
    """一次性票据消费（用后即焚）。原子性由 security.consume_ticket 保证。"""
    return _verdict(consume_ticket(payload.ticket))


# 兜底：/internal/** 下**任何**未匹配的路径与方法都回同一个 404。
# 目的有二：① 端点存在性不外泄（不出现 405 "存在但方法不对"）；
# ② 与 edge 的本地 404（/internal/** 不出网）逐字一致 —— 两条路径上
# 同一个探测请求的响应完全相同，便于 route_parity 直接比对。
@router.api_route("/{_path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def internal_not_found(_path: str) -> None:
    raise HTTPException(status_code=404)
