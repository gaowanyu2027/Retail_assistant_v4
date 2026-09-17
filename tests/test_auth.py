"""鉴权不变量测试：口令策略 / 哈希 / 会话滑动续期（用**临时 SQLite**，不碰线上 auth.db）。

关键手法：把 `api.security.AUTH_DB_PATH` 指向临时文件并清掉缓存的连接，
就能在完全离线的情况下测真实的建表/迁移/会话逻辑（无需 MySQL）。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api import security as S  # noqa: E402
from api.security import MAX_PASSWORD_LEN  # noqa: E402  # B10 的上界定义在 security 里（无 AUTH_ 前缀）
from config.settings import (  # noqa: E402
    AUTH_MIN_PASSWORD_LEN,
    AUTH_SESSION_HOURS,
    AUTH_SESSION_MAX_HOURS,
)

FMT = "%Y-%m-%d %H:%M:%S"

_LAST_TMP = None                         # 最近一次用过的临时目录，_cleanup 时整目录删掉


def _writable_tmp_dir():
    """返回一个**确实可写**的临时目录。

    为什么不直接用 `tempfile.mkdtemp()`：它内部用 `os.mkdir(path, 0o700)` 建目录，
    在受限令牌/沙箱环境里进程对这种方式建出来的目录写不进去，SQLite 报
    "unable to open database file"（看着像鉴权代码坏了，其实只是目录不可写）。
    所以这里不直接用 mkdtemp，而是**逐个候选目录真实探测能否建库**：
    系统临时目录 → 项目内 `data/_tmp_tests`（`data/` 已在 .gitignore 里）。
    """
    last_err = None
    stamp = f"{os.getpid()}_{int(time.time() * 1000) % 1000000}"
    for i, base in enumerate((Path(tempfile.gettempdir()), PROJECT_ROOT / "data" / "_tmp_tests")):
        d = base / f"auth_test_{stamp}_{i}"
        try:
            d.mkdir(parents=True, exist_ok=True)      # 默认 mode，不要 0o700
        except OSError as e:
            last_err = e
            continue
        probe = d / "_probe.db"
        try:
            c = sqlite3.connect(str(probe))
            c.execute("CREATE TABLE p(x)")
            c.close()
        except sqlite3.Error as e:
            last_err = e
            shutil.rmtree(d, ignore_errors=True)      # 这个候选不可用，试下一个
            continue
        try:
            probe.unlink()
        except OSError:
            pass
        return d
    raise RuntimeError(f"找不到可写的临时目录用于鉴权测试（最后一次错误：{last_err}）")


def _fresh_db():
    """把鉴权库指到临时文件，返回路径（调用方负责清理）。"""
    global _LAST_TMP
    _LAST_TMP = _writable_tmp_dir()
    tmp = _LAST_TMP / "auth.db"
    S.AUTH_DB_PATH = str(tmp)
    S._conn = None                       # 丢掉缓存的连接，否则会继续用旧库
    S.init_auth_db()
    return tmp


def _cleanup():
    global _LAST_TMP
    try:
        if S._conn is not None:
            S._conn.close()
    except Exception:
        pass
    S._conn = None
    if _LAST_TMP is not None:            # 连临时目录一起删，别在磁盘上留垃圾
        shutil.rmtree(_LAST_TMP, ignore_errors=True)
        _LAST_TMP = None


# ---------------- 口令策略（B10） ----------------

def test_password_policy_bounds():
    _fresh_db()
    try:
        # 太短
        try:
            S._validate_password("a" * (AUTH_MIN_PASSWORD_LEN - 1), "u")
            raise AssertionError("短口令应当被拒")
        except ValueError:
            pass
        # 太长
        try:
            S._validate_password("a" * (MAX_PASSWORD_LEN + 1), "u")
            raise AssertionError("超长口令应当被拒")
        except ValueError:
            pass
        # 纯空白
        try:
            S._validate_password(" " * (AUTH_MIN_PASSWORD_LEN + 2), "u")
            raise AssertionError("纯空白口令应当被拒")
        except ValueError:
            pass
        # 合法口令放行
        assert S._validate_password("S3cure-Pass-9x", "u") == "S3cure-Pass-9x"
    finally:
        _cleanup()


def test_hash_is_salted_and_verifies():
    _fresh_db()
    try:
        h1 = S.hash_password("Same-Pass-123")
        h2 = S.hash_password("Same-Pass-123")
        assert h1 != h2, "同一口令两次哈希必须不同（每用户随机盐）"
        assert h1.split("$")[0] == "pbkdf2_sha256"
        assert len(h1.split("$")) == 4
        assert S.verify_password("Same-Pass-123", h1) is True
        assert S.verify_password("wrong", h1) is False
        assert S.verify_password("Same-Pass-123", "garbage") is False
    finally:
        _cleanup()


# ---------------- 会话：滑动续期 + 绝对上限 ----------------

def _mk_session():
    user = S.create_user("t_user", "Test-Pass-9x", S.ROLE_PLATFORM, "测试")
    user = S.authenticate("t_user", "Test-Pass-9x")
    return S.create_session(user["id"], user["username"], user["role"], ip="10.0.0.1",
                            user_agent="pytest/1.0")["token"]


def _row(token):
    c = sqlite3.connect(S.AUTH_DB_PATH)
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM auth_session WHERE token=?", (token,)).fetchone()
    c.close()
    return r


def _set_times(token, created=None, expires=None):
    c = sqlite3.connect(S.AUTH_DB_PATH)
    if created:
        c.execute("UPDATE auth_session SET created_at=? WHERE token=?", (created, token))
    if expires:
        c.execute("UPDATE auth_session SET expires_at=? WHERE token=?", (expires, token))
    c.commit()
    c.close()


def test_session_records_source():
    _fresh_db()
    try:
        tok = _mk_session()
        r = _row(tok)
        assert r["ip"] == "10.0.0.1"
        assert r["user_agent"] == "pytest/1.0"
        assert r["last_seen_at"], "应记录最后活跃时间"
    finally:
        _cleanup()


def test_sliding_renewal_extends_and_caps():
    _fresh_db()
    try:
        tok = _mk_session()
        # 剩余时间充足 → 不续期
        _set_times(tok, expires=(datetime.now() + timedelta(hours=10)).strftime(FMT))
        before = _row(tok)["expires_at"]
        assert S.get_session(tok) is not None
        assert _row(tok)["expires_at"] == before, "离到期还早不该写库"

        # 剩余不足阈值 → 续期（注意时间戳精度只到秒，先睡 1.1s 才能看出变化）
        time.sleep(1.1)
        _set_times(tok, expires=(datetime.now() + timedelta(minutes=30)).strftime(FMT))
        assert S.get_session(tok) is not None
        r = _row(tok)
        new_exp = datetime.strptime(r["expires_at"], FMT)
        assert new_exp > datetime.now() + timedelta(hours=AUTH_SESSION_HOURS - 1), r["expires_at"]

        # 绝对上限：created_at 已在 23.5 小时前 → 续期不得越过 created+MAX
        old_created = (datetime.now() - timedelta(hours=AUTH_SESSION_MAX_HOURS - 0.5)).strftime(FMT)
        _set_times(tok, created=old_created,
                   expires=(datetime.now() + timedelta(minutes=10)).strftime(FMT))
        assert S.get_session(tok) is not None
        cap = datetime.strptime(old_created, FMT) + timedelta(hours=AUTH_SESSION_MAX_HOURS)
        got = datetime.strptime(_row(tok)["expires_at"], FMT)
        assert got <= cap + timedelta(seconds=1), f"越过了绝对上限: {got} > {cap}"
    finally:
        _cleanup()


def test_expired_session_rejected_and_deleted():
    _fresh_db()
    try:
        tok = _mk_session()
        _set_times(tok, expires=(datetime.now() - timedelta(minutes=1)).strftime(FMT))
        assert S.get_session(tok) is None, "过期会话必须被拒"
        assert _row(tok) is None, "过期行应被清理"
    finally:
        _cleanup()


def test_session_public_id_and_revoke():
    _fresh_db()
    try:
        tok = _mk_session()
        pid = S.session_public_id(tok)
        assert len(pid) == 12 and pid != tok, "对外只能是不可逆短标识"
        assert S.session_public_id(tok) == pid, "同一 token 必须稳定"
        active = S.list_active_sessions("t_user")
        assert len(active) == 1 and active[0]["id"] == pid
        assert "token" not in active[0], "会话列表绝不能返回 token"
        # 越权吊销：换个归属应失败
        assert S.revoke_session_by_public_id(pid, username="someone_else") is False
        assert S.revoke_session_by_public_id(pid, username="t_user") is True
        assert S.get_session(tok) is None
    finally:
        _cleanup()


def test_password_change_revokes_sessions():
    """改密必须吊销该用户全部会话（在线路径的安全语义）。"""
    _fresh_db()
    try:
        tok = _mk_session()
        user = S.get_user_by_username("t_user")
        S.update_user(user["id"], password="Another-Pass-9x")
        assert S.get_session(tok) is None, "改密后旧会话应立即失效"
    finally:
        _cleanup()
