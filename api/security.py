"""
登录鉴权 — 账号密码 + 服务端会话 + 角色权限

设计要点：
1. **零新增依赖**：口令哈希用标准库 hashlib.pbkdf2_hmac（PBKDF2-HMAC-SHA256 + 每用户随机盐），
   会话令牌用 secrets.token_urlsafe 生成（不可猜测）。
2. **会话存服务端**（SQLite）：可即时吊销——改密码/改角色/停用账号后旧会话立即失效，
   不需要 JWT 那套黑名单机制。
3. **独立于业务库**：鉴权数据放 data/auth.db，不用 MySQL。理由：业务库故障时
   不应把所有人（含管理员）锁在系统外，否则无法登录排查。
4. **双通道令牌**：
   - 浏览器：HttpOnly Cookie（同源请求与 WebSocket 握手自动携带，前端零改动）
   - 小程序/脚本：`Authorization: Bearer <token>`

角色权限：
    root     —— 平台管理员：数据读写 + 用户管理 + 系统管理
    platform —— 业务账户：数据读写（不可用用户/系统级功能）
"""
import hashlib
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta

from config.settings import (
    AUTH_COOKIE_SECURE,
    AUTH_DB_PATH,
    AUTH_FAILED_WINDOW_SECONDS,
    AUTH_IP_ATTEMPT_MULTIPLIER,
    AUTH_LOCKOUT_MAX_SECONDS,
    AUTH_LOCKOUT_SECONDS,
    AUTH_MAX_FAILED_ATTEMPTS,
    AUTH_MIN_PASSWORD_LEN,
    AUTH_PBKDF2_ITERATIONS,
    AUTH_PUBLIC_DOCS,
    AUTH_ALLOW_QUERY_TOKEN,
    AUTH_TICKET_TTL_SECONDS,
    AUTH_ROOT_PASSWORD,
    AUTH_ROOT_USERNAME,
    AUTH_SESSION_HOURS,
    AUTH_SESSION_MAX_HOURS,
    AUTH_SESSION_RENEW_THRESHOLD_HOURS,
    AUTH_SESSION_TOUCH_SECONDS,
    AUTH_SQLITE_JOURNAL_MODE,
    AUTH_TRUST_FORWARDED_FOR,
)

# ==================== 角色与权限 ====================

ROLE_ROOT = "root"
ROLE_PLATFORM = "platform"

ROLES = (ROLE_ROOT, ROLE_PLATFORM)

ROLE_LABELS = {
    ROLE_ROOT: "平台管理员（root）",
    ROLE_PLATFORM: "平台账户",
}

# 权限矩阵：新增权限点时在此登记
PERMISSIONS: dict[str, set[str]] = {
    ROLE_ROOT: {"data:read", "data:write", "user:manage", "system:manage"},
    ROLE_PLATFORM: {"data:read", "data:write"},
}

# Cookie 名（浏览器通道）
COOKIE_NAME = "retail_sid"

# 口令策略（长度 + 弱口令黑名单；不搞复杂组合规则——OWASP 更推荐长度与黑名单）
MIN_PASSWORD_LEN = AUTH_MIN_PASSWORD_LEN
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 32
# 口令最大长度：见 _validate_password 的说明（防超长口令放大哈希开销）
MAX_PASSWORD_LEN = 128

# 常见弱口令 / 与本产品相关的易猜口令（小写比较）
_WEAK_PASSWORDS = {
    "12345678", "123456789", "1234567890", "87654321", "98765432",
    "11111111", "00000000", "88888888", "66666666",
    "password", "passw0rd", "p@ssw0rd", "password1", "password123",
    "qwertyui", "qwerty123", "1qaz2wsx", "zxcvbnm1",
    "admin123", "admin888", "administrator", "root1234", "root12345",
    "abc12345", "abcd1234", "a1234567", "12345678a",
    "iloveyou", "letmein1", "welcome1", "sunshine",
    "retail123", "shop1234", "store123", "dazuoye1",
}

_PBKDF2_ITERATIONS = AUTH_PBKDF2_ITERATIONS


def has_perm(role: str, perm: str) -> bool:
    """角色是否具备某权限。"""
    return perm in PERMISSIONS.get(role, set())


def perms_of(role: str) -> list[str]:
    return sorted(PERMISSIONS.get(role, set()))


# ==================== 存储 ====================

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


# 允许的 journal 模式白名单（值来自环境变量，必须校验，避免拼进 PRAGMA 造成注入）
_JOURNAL_MODES = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        d = os.path.dirname(AUTH_DB_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        _conn = sqlite3.connect(AUTH_DB_PATH, check_same_thread=False, timeout=10.0)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA busy_timeout=5000")
        # journal 模式见 config/settings.py 的说明：
        # 默认 DELETE —— WAL 需要 mmap `-shm`，在 Windows bind mount（9p）上
        # **容器重建后**会直接 disk I/O error，把登录整条链路带崩。
        mode = AUTH_SQLITE_JOURNAL_MODE if AUTH_SQLITE_JOURNAL_MODE in _JOURNAL_MODES else "DELETE"
        try:
            _conn.execute(f"PRAGMA journal_mode={mode}")
        except sqlite3.Error as e:
            # 设不上也不能让鉴权起不来：退回默认回滚日志模式
            print(f"[Auth][WARN] 设置 journal_mode={mode} 失败（{e}），改用 DELETE")
            try:
                _conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error as e2:
                print(f"[Auth][ERROR] 连 DELETE 模式都设不上: {e2}")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def init_auth_db() -> None:
    """建表（幂等）。"""
    with _lock:
        conn = _connect()
        conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_session (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            ip TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_user ON auth_session(user_id)")
        # ---- 迁移：给已有的 auth_session 补 ip / user_agent / last_seen_at ----
        # 为什么需要：修复前会话表**不记录来源**，管理员无法回答
        # 「这条登录态是哪台机器/哪个浏览器」，也不能做"登出其他设备"。
        # SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，用 PRAGMA 判断（同 long_term_memory 的做法）。
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(auth_session)")}
        for _name, _ddl in (
            ("ip", "ALTER TABLE auth_session ADD COLUMN ip TEXT NOT NULL DEFAULT ''"),
            ("user_agent", "ALTER TABLE auth_session ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''"),
            ("last_seen_at", "ALTER TABLE auth_session ADD COLUMN last_seen_at TEXT"),
        ):
            if _name not in _cols:
                conn.execute(_ddl)
        # 登录失败计数（防暴力破解）：scope 形如 "u:用户名|ip:1.2.3.4" 或 "ip:1.2.3.4"
        conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_login_attempt (
            scope TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            lock_level INTEGER NOT NULL DEFAULT 0,
            first_failed_at TEXT NOT NULL,
            last_failed_at TEXT NOT NULL,
            locked_until TEXT
        )""")
        # 一次性票据：给 WS / 音频等「无法自定义请求头」的场景用（短时效 + 用后即焚）。
        # 这样即便票据出现在 URL 并落入访问日志，也已经是无效值。
        conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_ticket (
            ticket TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            purpose TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_exp ON auth_ticket(expires_at)")
        conn.commit()


# ==================== 口令哈希 ====================

def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 + 随机盐，格式：pbkdf2_sha256$iter$salt_hex$hash_hex"""
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令（常量时间比较，防时序侧信道）。"""
    try:
        algo, iter_s, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"),
            bytes.fromhex(salt_hex), int(iter_s),
        )
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ==================== 用户 ====================

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _validate_username(username: str) -> str:
    u = (username or "").strip()
    if not (MIN_USERNAME_LEN <= len(u) <= MAX_USERNAME_LEN):
        raise ValueError(f"用户名长度需在 {MIN_USERNAME_LEN}~{MAX_USERNAME_LEN} 之间")
    if not all(c.isalnum() or c in "_-.@" for c in u):
        raise ValueError("用户名只能包含字母、数字、下划线、点、@ 或短横线")
    return u


def _validate_password(password: str, username: str = "") -> str:
    """口令强度校验（长度 + 弱口令黑名单 + 非纯数字 + 不与用户名相同）。

    说明：不强制大小写/符号组合——OWASP 更推荐「足够长度 + 黑名单」，
    因为强制组合规则会促使用户产生 `Password1!` 这类可预测口令。

    ⚠ 两个**曾经漏判**的情况（实测都被接受）：
    1. **纯空白**：`'        '`（9 个空格）长度达标、`strip()` 后为空、
       既不在弱口令表里也不是纯数字 —— 一路通过。等于允许空口令。
    2. **超长**：`'a'*200` 也被接受。PBKDF2 没有 bcrypt 那样的 72 字节上限，
       但超长口令会让每次校验的哈希开销线性增长（可被用作放大攻击），
       且没有任何实际强度收益。这里按 OWASP 建议给一个宽松上界。
    """
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(password) > MAX_PASSWORD_LEN:
        raise ValueError(f"密码最多 {MAX_PASSWORD_LEN} 位")
    if not password.strip():
        raise ValueError("密码不能只包含空白字符")
    low = password.strip().lower()
    if low in _WEAK_PASSWORDS:
        raise ValueError("密码过于常见，请更换")
    if password.isdigit():
        raise ValueError("密码不能为纯数字")
    if username and low == username.strip().lower():
        raise ValueError("密码不能与用户名相同")
    return password


def _validate_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"角色必须是 {' / '.join(ROLES)} 之一")
    return role


def _row_to_user(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "role": row["role"],
        "role_label": ROLE_LABELS.get(row["role"], row["role"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "permissions": perms_of(row["role"]),
    }


def count_users() -> int:
    with _lock:
        return _connect().execute("SELECT COUNT(*) AS c FROM auth_user").fetchone()["c"]


def count_roots() -> int:
    with _lock:
        return _connect().execute(
            "SELECT COUNT(*) AS c FROM auth_user WHERE role=? AND enabled=1",
            (ROLE_ROOT,),
        ).fetchone()["c"]


def create_user(username: str, password: str, role: str = ROLE_PLATFORM,
                display_name: str = "") -> dict:
    """创建用户（用户名唯一）。"""
    username = _validate_username(username)
    password = _validate_password(password, username)
    role = _validate_role(role)
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM auth_user WHERE username=?", (username,)).fetchone():
            raise ValueError(f"用户名已存在: {username}")
        cur = conn.execute(
            "INSERT INTO auth_user (username, display_name, role, password_hash,"
            " enabled, created_at) VALUES (?,?,?,?,1,?)",
            (username, display_name or username, role, hash_password(password), _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM auth_user WHERE id=?", (cur.lastrowid,)).fetchone()
        return _row_to_user(row)


def get_user_by_username(username: str) -> dict | None:
    with _lock:
        row = _connect().execute(
            "SELECT * FROM auth_user WHERE username=?", ((username or "").strip(),)
        ).fetchone()
    return _row_to_user(row)


def get_user(user_id: int) -> dict | None:
    with _lock:
        row = _connect().execute("SELECT * FROM auth_user WHERE id=?", (int(user_id),)).fetchone()
    return _row_to_user(row)


def list_users() -> list[dict]:
    with _lock:
        rows = _connect().execute("SELECT * FROM auth_user ORDER BY id").fetchall()
    return [_row_to_user(r) for r in rows]


def update_user(user_id: int, *, role: str | None = None, password: str | None = None,
                enabled: bool | None = None, display_name: str | None = None) -> dict:
    """更新用户；改角色/密码/停用都会吊销其现有会话。"""
    user_id = int(user_id)
    cur = get_user(user_id)
    if not cur:
        raise ValueError(f"用户不存在: {user_id}")

    sets, args = [], []
    revoke = False
    if role is not None:
        role = _validate_role(role)
        # 保护：不能把最后一个可用 root 降级（否则系统再没人能管用户）
        if cur["role"] == ROLE_ROOT and role != ROLE_ROOT and count_roots() <= 1:
            raise ValueError("系统至少需保留一个可用的 root 账号")
        sets.append("role=?"); args.append(role); revoke = True
    if password is not None:
        sets.append("password_hash=?")
        args.append(hash_password(_validate_password(password, cur["username"])))
        revoke = True
    if enabled is not None:
        if not enabled and cur["role"] == ROLE_ROOT and count_roots() <= 1:
            raise ValueError("系统至少需保留一个可用的 root 账号")
        sets.append("enabled=?"); args.append(1 if enabled else 0); revoke = True
    if display_name is not None:
        sets.append("display_name=?"); args.append(display_name)

    if not sets:
        return cur

    with _lock:
        conn = _connect()
        conn.execute(f"UPDATE auth_user SET {', '.join(sets)} WHERE id=?", (*args, user_id))
        conn.commit()
    if revoke:
        revoke_user_sessions(user_id)
    return get_user(user_id)


def delete_user(user_id: int) -> None:
    user_id = int(user_id)
    cur = get_user(user_id)
    if not cur:
        raise ValueError(f"用户不存在: {user_id}")
    if cur["role"] == ROLE_ROOT and count_roots() <= 1:
        raise ValueError("系统至少需保留一个可用的 root 账号")
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM auth_user WHERE id=?", (user_id,))
        conn.commit()
    revoke_user_sessions(user_id)


# ==================== 会话 ====================

def create_session(user_id: int, username: str, role: str,
                   ip: str = "", user_agent: str = "") -> dict:
    """创建登录会话。

    `ip` / `user_agent`（2026-09 新增）：记录来源，便于回答
    「这条登录态是哪台机器/哪个浏览器」并支持登出其他设备。
    只做**审计展示**用，不参与任何安全判定（IP 可伪造、UA 可伪造）。
    """
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(hours=AUTH_SESSION_HOURS)
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO auth_session"
            " (token, user_id, username, role, created_at, expires_at, ip, user_agent, last_seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (token, int(user_id), username, role,
             now.strftime("%Y-%m-%d %H:%M:%S"), expires.strftime("%Y-%m-%d %H:%M:%S"),
             (ip or "")[:64], (user_agent or "")[:300], now.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    return {"token": token, "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S")}


def _parse_ts(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _maybe_extend_session(conn, row, now: datetime) -> None:
    """滑动续期（空闲超时 + **绝对上限** + 写限流）。

    三条规则（都可配，见 config/settings.py）：

    1. **空闲超时** `AUTH_SESSION_HOURS`（默认 12h）：有活动就把到期时间往后推；
    2. **绝对上限** `AUTH_SESSION_MAX_HOURS`（默认 24h，从 `created_at` 起算）：
       续期永远不越过它。没有这条，会话可以无限续命 —— 滑动窗口的经典漏洞；
    3. **写限流** `AUTH_SESSION_TOUCH_SECONDS`（默认 60s）+ 仅当剩余时间不足
       `AUTH_SESSION_RENEW_THRESHOLD_HOURS` 时才动。这一步是必须的：
       `get_session` 跑在**每个请求**上（前端 5 秒轮询、WS 握手、心跳），
       不加限流就等于每次请求都写一次 SQLite。
    """
    created = _parse_ts(row["created_at"])
    expires = _parse_ts(row["expires_at"])
    if not created or not expires:
        return
    hard_cap = created + timedelta(hours=AUTH_SESSION_MAX_HOURS)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    last_seen = _parse_ts(row["last_seen_at"])
    throttled = bool(last_seen) and (now - last_seen).total_seconds() < AUTH_SESSION_TOUCH_SECONDS

    # 1) 续期（**不受节流限制**）
    #    节流只该约束"无害的 last_seen 刷新"，绝不能挡住"再不续期就要掉线"的续期 ——
    #    第一版把两者混在一个节流里，结果是刚创建的会话（last_seen=刚刚）即使只剩
    #    30 分钟也不会被续期（实测踩到）。这里按"剩余不足阈值 + 未到绝对上限"判断，
    #    天然就把续期写限制在**每个会话每（空闲-阈值）小时一次**（默认 10 小时一次），
    #    不需要额外节流。
    if now < hard_cap and \
            (expires - now).total_seconds() <= AUTH_SESSION_RENEW_THRESHOLD_HOURS * 3600:
        new_expires = min(now + timedelta(hours=AUTH_SESSION_HOURS), hard_cap)
        if new_expires > expires:
            conn.execute(
                "UPDATE auth_session SET expires_at=?, last_seen_at=? WHERE token=?",
                (new_expires.strftime("%Y-%m-%d %H:%M:%S"), stamp, row["token"]),
            )
            conn.commit()
            return

    # 2) 不需要续期（或已到绝对上限）：只按节流刷新"最后活跃时间"，
    #    否则每个请求（前端 5 秒轮询、WS 握手）都会变成一次 SQLite 写。
    if not throttled:
        conn.execute(
            "UPDATE auth_session SET last_seen_at=? WHERE token=?", (stamp, row["token"])
        )
        conn.commit()


def get_session(token: str | None) -> dict | None:
    """按令牌取会话（含用户最新状态）；过期/失效/账号停用返回 None。

    额外做滑动续期：见 `_maybe_extend_session`（有时间上限，不会无限续命）。
    """
    if not token:
        return None
    now = datetime.now()
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM auth_session WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        if row["expires_at"] <= _now():
            conn.execute("DELETE FROM auth_session WHERE token=?", (token,))
            conn.commit()
            return None
        try:
            _maybe_extend_session(conn, row, now)
        except Exception as e:                  # 续期失败绝不能影响鉴权主流程
            print(f"[Auth][WARN] 会话滑动续期失败（本次请求不受影响）: {e}")
        expires_at = row["expires_at"]
    user = get_user(row["user_id"])
    if not user or not user["enabled"]:
        return None
    # 角色以用户表为准（改角色会吊销会话，这里再兜一层）
    user = dict(user)
    user["token"] = token
    user["session_expires_at"] = expires_at
    return user


def session_public_id(token: str | None) -> str:
    """会话的对外短标识（不可逆）：会话列表里用它代替 token。

    token 等价于密码，**任何列表接口都不该把它吐出来**；这里给 12 位 sha256 前缀，
    足够在 UI 里区分与定位（含 Logout 该设备），又无法反推原 token。
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def list_active_sessions(username: str | None = None) -> list[dict]:
    """列出**未过期**会话（`username=None` = 全部账号，审计视角）。

    返回**不含 token**（用 `session_public_id` 替代）。
    """
    now_s = _now()
    out = []
    with _lock:                                 # 共享连接：读也要持锁（其余函数同约定）
        conn = _connect()
        if username:
            rows = conn.execute(
                "SELECT * FROM auth_session WHERE username=? AND expires_at>? "
                "ORDER BY created_at DESC",
                (username, now_s),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM auth_session WHERE expires_at>? ORDER BY created_at DESC",
                (now_s,),
            ).fetchall()
    for r in rows:
        out.append({
            "id": session_public_id(r["token"]),
            "username": r["username"],
            "role": r["role"],
            "ip": r["ip"] or "",
            "user_agent": r["user_agent"] or "",
            "created_at": r["created_at"],
            "last_seen_at": r["last_seen_at"] or "",
            "expires_at": r["expires_at"],
        })
    return out


def revoke_session_by_public_id(public_id: str, username: str | None = None) -> bool:
    """按对外短标识吊销会话（`username` 非空时限定归属）。

    实现上是全表比对 sha256 前缀 —— 会话表规模很小（个位数~几十行），
    这样比额外存一列 id 更简单，也不会因为多一列而需要再迁移一次。
    """
    if not public_id:
        return False
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT token, username FROM auth_session").fetchall()
        for r in rows:
            if session_public_id(r["token"]) != public_id:
                continue
            if username is not None and (r["username"] or "") != username:
                return False
            conn.execute("DELETE FROM auth_session WHERE token=?", (r["token"],))
            conn.commit()
            return True
    return False


def revoke_session(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM auth_session WHERE token=?", (token,))
        conn.commit()
        return cur.rowcount > 0


def revoke_user_sessions(user_id: int) -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM auth_session WHERE user_id=?", (int(user_id),))
        conn.commit()
        return cur.rowcount


def purge_expired_sessions() -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM auth_session WHERE expires_at <= ?", (_now(),))
        conn.commit()
        return cur.rowcount


def mark_login(user_id: int) -> None:
    with _lock:
        conn = _connect()
        conn.execute("UPDATE auth_user SET last_login_at=? WHERE id=?", (_now(), int(user_id)))
        conn.commit()


def authenticate(username: str, password: str) -> dict | None:
    """校验账号密码；成功返回用户，失败返回 None（不区分「用户不存在/密码错」）。"""
    user = get_user_by_username(username)
    if not user or not user["enabled"]:
        # 仍然做一次哈希运算，避免用响应时间枚举用户名
        verify_password(password, hash_password("dummy-timing-equalizer"))
        return None
    with _lock:
        row = _connect().execute(
            "SELECT password_hash FROM auth_user WHERE id=?", (user["id"],)
        ).fetchone()
    if not verify_password(password, row["password_hash"]):
        return None
    mark_login(user["id"])
    return user


# ==================== 登录限流（防暴力破解） ====================
#
# 设计要点：
# - 计数存服务端（SQLite），重启不丢、跨进程一致
# - 主计数维度是「用户名 + IP」：这样攻击者无法通过狂试某账号把该账号的
#   **正常用户**（不同 IP）一起锁在门外（避免"锁定即拒绝服务"）
# - 另有「IP 维度」的宽松上限（用户名上限 × 倍数），用于识别换用户名的撞库式试探
# - 锁定期间计数归零、由 locked_until 生效；重复触发时锁定时长按倍数递增（有上限）

def _scope_user(username: str, ip: str) -> str:
    return f"u:{username}|ip:{ip}"


def _scope_ip(ip: str) -> str:
    return f"ip:{ip}"


def _remaining_lock_seconds(conn, scope: str) -> int:
    row = conn.execute(
        "SELECT locked_until FROM auth_login_attempt WHERE scope=?", (scope,)
    ).fetchone()
    if not row or not row["locked_until"]:
        return 0
    try:
        until = datetime.strptime(row["locked_until"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return 0
    remain = (until - datetime.now()).total_seconds()
    return int(remain) if remain > 0 else 0


def check_login_allowed(username: str, ip: str = "") -> tuple[bool, int]:
    """当前是否允许尝试登录。返回 (是否允许, 还需等待秒数)。"""
    with _lock:
        conn = _connect()
        wait = _remaining_lock_seconds(conn, _scope_user(username, ip))
        if ip:
            wait = max(wait, _remaining_lock_seconds(conn, _scope_ip(ip)))
    return (wait == 0), wait


def record_login_failure(username: str, ip: str = "") -> int:
    """记一次登录失败；达到阈值则锁定。返回锁定秒数（0 = 未锁定）。"""
    now = datetime.now()
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    scopes = [(_scope_user(username, ip), AUTH_MAX_FAILED_ATTEMPTS)]
    if ip:
        scopes.append((_scope_ip(ip), AUTH_MAX_FAILED_ATTEMPTS * AUTH_IP_ATTEMPT_MULTIPLIER))

    locked_secs = 0
    with _lock:
        conn = _connect()
        for scope, limit in scopes:
            row = conn.execute(
                "SELECT * FROM auth_login_attempt WHERE scope=?", (scope,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO auth_login_attempt (scope, failed_count, lock_level,"
                    " first_failed_at, last_failed_at, locked_until)"
                    " VALUES (?,1,0,?,?,NULL)",
                    (scope, now_s, now_s),
                )
                continue
            try:
                first = datetime.strptime(row["first_failed_at"], "%Y-%m-%d %H:%M:%S")
                window_expired = (now - first).total_seconds() > AUTH_FAILED_WINDOW_SECONDS
            except Exception:
                window_expired = True

            count = 1 if window_expired else int(row["failed_count"]) + 1
            first_s = now_s if window_expired else row["first_failed_at"]
            lock_level = int(row["lock_level"] or 0)
            locked_until = None
            if count >= limit:
                lock_level += 1
                secs = min(AUTH_LOCKOUT_SECONDS * lock_level, AUTH_LOCKOUT_MAX_SECONDS)
                locked_until = (now + timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")
                count = 0          # 锁定期间由 locked_until 生效，计数归零重新开始
                locked_secs = max(locked_secs, secs)
            conn.execute(
                "UPDATE auth_login_attempt SET failed_count=?, lock_level=?,"
                " first_failed_at=?, last_failed_at=?, locked_until=? WHERE scope=?",
                (count, lock_level, first_s, now_s, locked_until, scope),
            )
        conn.commit()
    return locked_secs


def clear_login_failures(username: str, ip: str = "") -> None:
    """登录成功后清除失败计数（含该 IP 的宽松计数）。"""
    scopes = [_scope_user(username, ip)]
    if ip:
        scopes.append(_scope_ip(ip))
    with _lock:
        conn = _connect()
        conn.executemany(
            "DELETE FROM auth_login_attempt WHERE scope=?", [(s,) for s in scopes]
        )
        conn.commit()


def purge_login_attempts() -> int:
    """清理窗口外且已解锁的失败记录，避免表无限增长。"""
    cutoff = (datetime.now() - timedelta(seconds=AUTH_FAILED_WINDOW_SECONDS)
              ).strftime("%Y-%m-%d %H:%M:%S")
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "DELETE FROM auth_login_attempt WHERE last_failed_at < ?"
            " AND (locked_until IS NULL OR locked_until <= ?)",
            (cutoff, now_s),
        )
        conn.commit()
        return cur.rowcount


def login_with_throttle(username: str, password: str, ip: str = "") -> dict:
    """带限流的登录。

    Returns:
        {"ok": True,  "user": {...}}
        {"ok": False, "reason": "locked",          "retry_after": 秒}
        {"ok": False, "reason": "bad_credentials"}
    """
    allowed, wait = check_login_allowed(username, ip)
    if not allowed:
        print(f"[AuthAudit] 登录被限流 username={username!r} ip={ip} 需等待 {wait}s")
        return {"ok": False, "reason": "locked", "retry_after": wait}

    user = authenticate(username, password)
    if user:
        clear_login_failures(username, ip)
        return {"ok": True, "user": user}

    locked_for = record_login_failure(username, ip)
    print(f"[AuthAudit] 登录失败 username={username!r} ip={ip}"
          + (f" → 已锁定 {locked_for}s" if locked_for else ""))
    if locked_for:
        return {"ok": False, "reason": "locked", "retry_after": locked_for}
    return {"ok": False, "reason": "bad_credentials"}


# ==================== 一次性票据（WS / 音频等无法设请求头的场景） ====================
#
# 为什么需要：浏览器 WebSocket API 与音频元素都无法自定义请求头；小程序虽然
# wx.connectSocket 支持 header，但音频 src 同样不支持，且小程序没有 Cookie jar。
# 这些场景只能用 URL 传凭据——而 URL 会进访问日志（CWE-598）。
# 解法：URL 里不发主会话令牌，改发**短时效（默认 60s）+ 用后即焚**的票据，
# 即使落入日志也已失效。

def issue_ticket(user: dict, purpose: str = "") -> dict:
    """签发一次性票据（需已登录）。"""
    ticket = secrets.token_urlsafe(24)
    now = datetime.now()
    expires = now + timedelta(seconds=AUTH_TICKET_TTL_SECONDS)
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO auth_ticket (ticket, user_id, username, role, purpose,"
            " created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (ticket, int(user["id"]), user["username"], user["role"], purpose,
             now.strftime("%Y-%m-%d %H:%M:%S"), expires.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    return {
        "ticket": ticket,
        "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"),
        "ttl_seconds": AUTH_TICKET_TTL_SECONDS,
    }


def consume_ticket(ticket: str | None) -> dict | None:
    """校验并**消费**票据（用后即焚）。过期/不存在/已用过 → None。

    返回与 get_session 同构的用户 dict（含 token 字段便于复用）。
    """
    if not ticket:
        return None
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM auth_ticket WHERE ticket=?", (ticket,)).fetchone()
        if not row:
            return None
        # 一次性：无论后续校验是否通过，先删除（避免重放）
        conn.execute("DELETE FROM auth_ticket WHERE ticket=?", (ticket,))
        conn.commit()
    if row["expires_at"] <= now_s:
        print("[AuthAudit] 票据已过期被拒")
        return None
    user = get_user(row["user_id"])
    if not user or not user["enabled"]:
        return None
    user = dict(user)
    user["token"] = None                     # 票据不代表会话，不提供会话令牌
    user["via"] = "ticket"
    return user


def purge_expired_tickets() -> int:
    """清理过期票据（票据本就短时效，避免表增长）。"""
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM auth_ticket WHERE expires_at <= ?", (now_s,))
        conn.commit()
        return cur.rowcount


# ==================== 首次启动引导 ====================

def bootstrap_root() -> dict | None:
    """无任何用户时创建 root；返回创建信息（含一次性明文密码）或 None。

    若 AUTH_ROOT_PASSWORD 不满足口令策略，**回退为随机强口令并告警**——
    否则引导会抛错、root 建不出来，等于把所有人锁在系统外。
    """
    init_auth_db()
    if count_users() > 0:
        return None

    password = AUTH_ROOT_PASSWORD
    generated = False
    if password:
        try:
            _validate_password(password, AUTH_ROOT_USERNAME)
        except ValueError as e:
            print(f"[Auth] AUTH_ROOT_PASSWORD 不满足口令策略（{e}），改用随机强口令")
            password = ""
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True

    user = create_user(AUTH_ROOT_USERNAME, password, ROLE_ROOT, "平台管理员")
    return {"user": user, "password": password, "generated": generated}


# ==================== FastAPI 依赖 ====================

import json as _json  # noqa: E402
from urllib.parse import parse_qs  # noqa: E402

from fastapi import Depends, HTTPException, Request  # noqa: E402

from config.settings import AUTH_ENABLED  # noqa: E402


def token_from_request(request: Request) -> str | None:
    """从 Cookie / Authorization: Bearer 提取**会话令牌**（不含 URL 凭据）。"""
    tok = request.cookies.get(COOKIE_NAME)
    if tok:
        return tok
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def user_from_request(request: Request) -> dict | None:
    """按优先级解析请求凭据 → 用户。

    优先级：Cookie / Bearer（正常会话）→ 一次性票据 `?ticket=`。
    `?token=`（URL 里的主会话令牌）**默认拒绝**：会话令牌进 URL 会落入访问日志
    （CWE-598），需要 URL 传凭据的场景请改用 /api/auth/ws-ticket 换票据。
    """
    user = getattr(request.state, "user", None)
    if user:
        return user
    session_token = token_from_request(request)
    if session_token:
        return get_session(session_token)
    ticket = request.query_params.get("ticket")
    if ticket:
        return consume_ticket(ticket)
    qtok = request.query_params.get("token")
    if qtok:
        if AUTH_ALLOW_QUERY_TOKEN:          # 兼容模式（不推荐）
            return get_session(qtok)
        _warn_url_session_token()
    return None


def _warn_url_session_token() -> None:
    """拒绝 URL 里的主会话令牌并给出可操作提示（不打印令牌本身）。"""
    if AUTH_ALLOW_QUERY_TOKEN:
        return
    print("[AuthAudit] 拒绝 URL 中的主会话令牌（?token=）：会落入访问日志，"
          "请改用 POST /api/auth/ws-ticket 换取一次性票据（?ticket=）")


def cookie_secure(request: Request | None) -> bool:
    """会话 Cookie 是否加 Secure 标志（仅 https 传输）。

    - `AUTH_COOKIE_SECURE` 显式设 1/0 → 按其取值（强制）
    - 否则自动判定：
        1. 请求本身是 https → 加
        2. 否则看 `X-Forwarded-Proto`（反向代理 / 内网穿透场景下，
           外部访问是 https，但应用侧看到的 scheme 是 http）
    """
    if AUTH_COOKIE_SECURE is not None:
        return AUTH_COOKIE_SECURE
    if request is None:
        return False
    try:
        if request.url.scheme == "https":
            return True
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return proto == "https"
    except Exception:
        return False


def client_ip_from_request(request: Request | None) -> str:
    """取**审计展示**用的来源 IP。

    默认取直连对端（`request.client.host`）；只有显式打开
    `AUTH_TRUST_FORWARDED_FOR=1` 时才采信 `X-Forwarded-For` 的第一跳
    （反向代理场景，此时对端是代理 IP，没意义）。

    ⚠ 这个值可以伪造（XFF 头由客户端随意填），因此**只用于会话列表展示**：
    登录限流仍使用直连对端 IP（见 `api/routes/auth.py` 的 login）。
    """
    if request is None:
        return ""
    try:
        if AUTH_TRUST_FORWARDED_FOR:
            xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            if xff:
                return xff
        return (request.client.host if request.client else "") or ""
    except Exception:
        return ""


async def get_current_user(request: Request) -> dict:
    """当前登录用户；未登录抛 401。"""
    user = user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


async def get_optional_user(request: Request) -> dict | None:
    """当前登录用户；未登录返回 None（不抛异常）。

    用于公开端点按登录与否返回不同粒度（如 /api/health）。
    """
    return user_from_request(request)


def require_perm(perm: str):
    """权限依赖工厂：require_perm("user:manage")。"""
    async def _dep(user: dict = Depends(get_current_user)) -> dict:
        if not has_perm(user.get("role", ""), perm):
            raise HTTPException(
                status_code=403,
                detail=("权限不足：该操作需要 "
                        f"{perm}（当前角色 {ROLE_LABELS.get(user.get('role'), user.get('role'))}）"),
            )
        return user
    return _dep


# ==================== ASGI 中间件（HTTP + WebSocket 全覆盖） ====================

# 无需登录即可访问：登录接口本身、登出、健康检查、前端加载失败上报
# 接口文档默认**不公开**——对已上鉴权的系统，公开完整接口清单（端点+参数）等于
# 给攻击者一张地图。登录用户仍可正常访问（同源 Cookie 自动携带）。
# 确需公开时设 AUTH_PUBLIC_DOCS=1。
_PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",       # 幂等：未登录也算成功，便于客户端清理本地状态
    "/api/health",            # liveness：只证明进程在跑
    "/api/health/ready",      # readiness：真实探测依赖（探针无法带凭据，必须公开）
    "/api/frontend/fallback",
}
_DOC_PREFIXES = ("/docs", "/redoc", "/openapi.json")


def needs_auth(path: str) -> bool:
    """默认封启策略：/api/* 一律需要登录（白名单除外）。

    另外接口文档（/docs、/redoc、/openapi.json）不在 /api/ 前缀下，
    必须显式要求登录——否则只靠 "/api/ 前缀" 这条会把文档漏成公开。
    静态资源与 SPA 入口放行，否则连登录页都加载不出来。
    """
    if path in _PUBLIC_PATHS:
        return False
    if path.startswith(_DOC_PREFIXES):
        return not AUTH_PUBLIC_DOCS      # 文档默认需登录
    return path.startswith("/api/")


def _scope_headers(scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in scope.get("headers") or []:
        try:
            out[k.decode("latin-1").lower()] = v.decode("latin-1")
        except Exception:
            pass
    return out


def token_from_scope(scope) -> str | None:
    """ASGI scope 层取**会话令牌**（Cookie / Authorization: Bearer，不含 URL 凭据）。

    WebSocket 说明：浏览器 WebSocket API 不支持自定义请求头，
    但同源 Cookie 会在握手时自动携带，因此浏览器端无需在 URL 里带凭据。
    """
    headers = _scope_headers(scope)
    cookie = headers.get("cookie") or ""
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == COOKIE_NAME:
                return v.strip()
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _scope_query(scope) -> dict:
    try:
        return parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    except Exception:
        return {}


def user_from_scope(scope) -> dict | None:
    """ASGI scope 层解析用户（HTTP 与 WebSocket 通用）。

    优先级：Cookie / Bearer（会话）→ `?ticket=`（一次性票据）。
    `?token=`（URL 里的主会话令牌）**默认拒绝**，理由见 user_from_request。
    """
    session_token = token_from_scope(scope)
    if session_token:
        return get_session(session_token)
    qs = _scope_query(scope)
    if qs.get("ticket"):
        return consume_ticket(qs["ticket"][0])
    if qs.get("token"):
        if AUTH_ALLOW_QUERY_TOKEN:          # 兼容模式（不推荐）
            return get_session(qs["token"][0])
        _warn_url_session_token()
    return None


def _set_request_actor(user: dict | None) -> None:
    """把当前账号写进**请求上下文**，供会话归属过滤使用（B3）。

    为什么放在中间件而不是各路由：Agent 工具（`search_chat_history`）与向量记忆
    检索拿不到 request 对象，却能读到 ContextVar —— 它们正是最危险的泄露路径
    （会把别人的历史问答喂给 LLM）。详见 auth_context.py 的模块说明。
    """
    try:
        import auth_context  # 本地导入：root 级模块，避免启动期导入顺序问题
        if user:
            auth_context.set_actor(
                user.get("username"),
                audit=has_perm(user.get("role", ""), "system:manage"),
            )
        else:
            auth_context.set_actor(None)
    except Exception as e:                                  # 上下文失败不能拦请求
        print(f"[Auth] 请求上下文设置失败（会话过滤将退化为看不到有归属的数据）: {e}")


class AuthMiddleware:
    """鉴权中间件（纯 ASGI，同时覆盖 HTTP 与 WebSocket）。

    为什么不用 BaseHTTPMiddleware：它不拦截 WebSocket，
    视频流 / 汇报推送这类 WS 通道会绕过鉴权。
    """

    def __init__(self, app, enabled: bool | None = None):
        self.app = app
        self.enabled = AUTH_ENABLED if enabled is None else enabled

    async def __call__(self, scope, receive, send):
        stype = scope.get("type")
        if not self.enabled or stype not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path") or ""
        if not needs_auth(path):
            # 公开路径也尽量解析登录态：供 /api/health 这类端点按「是否已登录」
            # 返回不同粒度（匿名只给最小信息，避免设备/推理环境指纹外泄）。
            # 注意：这里只认 Cookie / Bearer，**不消费**一次性票据（避免无谓消耗）。
            user = get_session(token_from_scope(scope))
            if user:
                scope.setdefault("state", {})["user"] = user
            _set_request_actor(user)
            return await self.app(scope, receive, send)

        user = user_from_scope(scope)
        _set_request_actor(user)
        if not user:
            if stype == "websocket":
                # 先 accept 再以 1008（策略违规）关闭，客户端可拿到明确关闭码
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 1008})
                return
            body = _json.dumps({"detail": "未登录或登录已过期"},
                               ensure_ascii=False).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        scope.setdefault("state", {})["user"] = user
        return await self.app(scope, receive, send)
