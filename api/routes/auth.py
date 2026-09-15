"""
登录鉴权接口 — 账号密码登录（无验证码/手机号/微信）

- POST   /api/auth/login          账号密码登录（下发 HttpOnly Cookie，同时返回 token 供小程序/脚本用）
- POST   /api/auth/logout         登出（吊销当前会话）
- GET    /api/auth/me             当前登录用户 + 权限（前端登录门禁用）
- POST   /api/auth/password       修改自己的密码（改后其它会话失效）
- GET    /api/auth/users          用户列表            [root]
- POST   /api/auth/users          新建用户            [root]
- PATCH  /api/auth/users/{id}     改角色/密码/启停/昵称 [root]
- DELETE /api/auth/users/{id}     删除用户            [root]
- GET    /api/auth/roles          角色与权限矩阵（只读，供前端渲染）
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from api.security import (
    COOKIE_NAME,
    ROLE_LABELS,
    ROLE_ROOT,
    ROLES,
    create_session,
    create_user,
    cookie_secure,
    delete_user,
    get_current_user,
    issue_ticket,
    list_users,
    login_with_throttle,
    perms_of,
    require_perm,
    revoke_session,
    revoke_user_sessions,
    update_user,
)
from config.settings import AUTH_SESSION_HOURS

router = APIRouter(tags=["auth"])


# ==================== 请求模型 ====================

class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = Field("platform", description="root=平台管理员 / platform=平台账户")
    display_name: str = ""


class UpdateUserRequest(BaseModel):
    role: str | None = None
    password: str | None = None
    enabled: bool | None = None
    display_name: str | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class TicketRequest(BaseModel):
    """一次性票据签发请求（purpose 仅用于审计标注，可选）"""
    purpose: str = ""


def _set_session_cookie(response: Response, token: str,
                        request: Request | None = None) -> bool:
    """下发 HttpOnly 会话 Cookie，返回是否带 Secure（便于验证/日志）。

    httpOnly：JS 读不到，降低 XSS 窃取风险；
    SameSite=Lax：同源请求与 WebSocket 握手会携带，前端无需改动；跨站 POST 不带 Cookie；
    secure  ：https 下自动置位（也识别 X-Forwarded-Proto），避免会话 Cookie 在明文信道被截获。
    """
    secure = cookie_secure(request)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=AUTH_SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return secure


# ==================== 登录 / 登出 / 当前用户 ====================

@router.post("/auth/login")
async def login(req: LoginRequest, response: Response, request: Request):
    """账号密码登录（带失败限流，防暴力破解）。"""
    ip = (request.client.host if request.client else "") or ""
    result = login_with_throttle(req.username, req.password, ip)

    if not result.get("ok"):
        if result.get("reason") == "locked":
            wait = int(result.get("retry_after") or 0)
            raise HTTPException(
                status_code=429,
                detail=f"登录尝试过于频繁，请 {wait} 秒后重试",
                headers={"Retry-After": str(max(wait, 1))},
            )
        # 不区分「用户不存在 / 密码错误 / 账号停用」，避免账号枚举
        raise HTTPException(status_code=401, detail="用户名或密码错误，或账号已停用")

    user = result["user"]
    session = create_session(user["id"], user["username"], user["role"])
    secure = _set_session_cookie(response, session["token"], request)
    user = dict(user)
    user.pop("permissions", None)
    print(f"[AuthAudit] 登录成功 username={user['username']!r} role={user['role']}"
          f" ip={ip} cookie_secure={secure}")
    return {
        "token": session["token"],          # 小程序/脚本用（浏览器走 Cookie 即可）
        "expires_at": session["expires_at"],
        "user": user,
        "permissions": perms_of(user["role"]),
    }


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    """登出：吊销当前会话（未登录时也返回成功，幂等）。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    revoked = revoke_session(token) if token else False
    # 清除 Cookie 时带上与下发时一致的 secure 标志，确保各浏览器都能清干净
    response.delete_cookie(COOKIE_NAME, path="/", secure=cookie_secure(request),
                           httponly=True, samesite="lax")
    return {"code": 0, "msg": "已登出", "revoked": revoked}


@router.post("/auth/ws-ticket")
async def create_ws_ticket(req: TicketRequest | None = None,
                           user: dict = Depends(get_current_user)):
    """签发**一次性短时效票据**，用于 WS / 音频等无法自定义请求头的场景。

    为什么需要：浏览器 WebSocket API 与音频元素都无法设置请求头；小程序虽支持
    `wx.connectSocket` 的 header，但音频 src 同样不支持，且小程序没有 Cookie jar。
    这些场景只能把凭据放 URL —— 而 URL 会进访问日志（CWE-598）。
    因此**不要在 URL 里放主会话令牌**，改为先用本接口（走 Cookie/Bearer 正常鉴权）
    换取票据，再以 `?ticket=xxx` 访问；票据 60 秒有效、用后即焚，
    即使落入日志也已失效。

    用法：
        POST /api/auth/ws-ticket            → {"ticket": "..."}
        ws://host/api/ws/stream?ticket=xxx
        http://host/api/tts?text=hi&ticket=xxx
    """
    try:
        info = issue_ticket(user, purpose=(req.purpose if req else "") or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"票据签发失败: {str(e)}")
    return {"code": 0, "msg": "票据已签发（一次性，用后即焚）", **info}


@router.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    """当前登录用户（前端启动时调用：401 则显示登录页）。"""
    return {
        "user": {k: v for k, v in user.items()
                 if k not in ("token", "session_expires_at", "permissions")},
        "permissions": user.get("permissions", []),
        "session_expires_at": user.get("session_expires_at"),
    }


@router.post("/auth/password")
async def change_own_password(req: ChangePasswordRequest,
                              request: Request,
                              user: dict = Depends(get_current_user)):
    """修改自己的密码（改后吊销本人全部会话，需重新登录）。"""
    import api.security as sec

    if not sec.verify_password(req.old_password, _password_hash_of(user["id"])):
        raise HTTPException(status_code=400, detail="原密码不正确")
    try:
        sec.update_user(user["id"], password=req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"code": 0, "msg": "密码已修改，请重新登录"}


def _password_hash_of(user_id: int) -> str:
    import api.security as sec
    with sec._lock:  # noqa: SLF001 — 内部小工具，避免再开一个公开接口
        row = sec._connect().execute(  # noqa: SLF001
            "SELECT password_hash FROM auth_user WHERE id=?", (int(user_id),)
        ).fetchone()
    return row["password_hash"] if row else ""


@router.get("/auth/roles")
async def roles():
    """角色与权限矩阵（只读；供前端展示与按钮显隐）。"""
    return {
        "roles": [{"role": r, "label": ROLE_LABELS.get(r, r), "permissions": perms_of(r)}
                  for r in ROLES],
        "default_role": "platform",
    }


# ==================== 用户管理（root） ====================

@router.get("/auth/users")
async def get_users(_: dict = Depends(require_perm("user:manage"))):
    return {"users": list_users()}


@router.post("/auth/users")
async def post_user(req: CreateUserRequest,
                    _: dict = Depends(require_perm("user:manage"))):
    try:
        user = create_user(req.username, req.password, req.role, req.display_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"code": 0, "msg": f"已创建用户 {user['username']}", "user": user}


@router.patch("/auth/users/{user_id}")
async def patch_user(user_id: int, req: UpdateUserRequest,
                     admin: dict = Depends(require_perm("user:manage"))):
    if user_id == admin["id"] and req.enabled is False:
        raise HTTPException(status_code=400, detail="不能停用当前登录账号")
    try:
        user = update_user(
            user_id,
            role=req.role,
            password=req.password,
            enabled=req.enabled,
            display_name=req.display_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"code": 0, "msg": f"已更新用户 {user['username']}", "user": user}


@router.delete("/auth/users/{user_id}")
async def remove_user(user_id: int,
                      admin: dict = Depends(require_perm("user:manage"))):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    try:
        delete_user(user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"code": 0, "msg": "已删除"}
