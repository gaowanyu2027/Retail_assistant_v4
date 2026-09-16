"""离线重置账号口令（忘记密码 / 登不进去时用）

## 为什么需要它

在线路径（`POST /api/auth/password`、UI）做不到"进不去"的场景；而**直接改 SQLite** 有两个坑：

1. `password_hash` 是 `pbkdf2_sha256$<迭代>$<盐>$<哈希>`，SQLite 没有 PBKDF2 函数，写不了明文；
2. **改哈希不会让已有会话失效** —— 会话校验只看 `auth_session` 表，根本不读 `password_hash`。
   手改等于"改了密码但没踢下线"，正是最想解决的问题。

本脚本走完整流程：**备份 → 校验策略 → 写哈希 → 吊销该账号全部会话**，
哈希与策略复用 `api/security.py` 的实现，保证与在线路径**完全一致**。

## 用法（务必先停后端，避免写锁冲突）

    docker compose stop backend
    python tools/reset_password.py --user root          # 交互式输入，不回显、不进 shell 历史
    docker compose start backend

    # 查看现有账号：
    python tools/reset_password.py --list
    # 自动化场景（⚠ 会进 shell 历史，且可能被 ps 看到）：
    python tools/reset_password.py --user root --password 'xxxx'

## 注意

- 备份文件 `data/auth.db.bak-<时间戳>` 落在同目录；**确认新口令能登录后再删**。
- 口令策略与在线一致：≥8 位、≤128 位、不能只含空白字符（见 AUTH_MIN_PASSWORD_LEN / B10）。
- 脚本默认**吊销该账号全部会话**（`--keep-sessions` 可关闭，一般不推荐）。
"""
import argparse
import getpass
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB = PROJECT_ROOT / "data" / "auth.db"


def _load_security():
    """复用在线路径的哈希与口令策略实现（保证行为一致，不自造一套）。"""
    from api import security as S
    return S


def _backup(db_path: Path) -> Path:
    """备份鉴权库。

    ⚠ 若该库处于 **WAL** 模式，数据可能全在 `-wal` 里，只复制主文件会得到"空库"
    （E10 事故的教训）；所以这里连同 `-wal`/`-shm` 一起复制。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db_path.with_name(db_path.name + f".bak-{stamp}")
    shutil.copy2(db_path, dest)
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            shutil.copy2(side, dest.with_name(dest.name + suffix))
    return dest


def _list_users(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, username, display_name, role, enabled, last_login_at FROM auth_user ORDER BY id"
    ).fetchall()
    print(f"  {'id':<4}{'用户名':<16}{'昵称':<16}{'角色':<10}{'启用':<6}最后登录")
    for r in rows:
        print(f"  {r[0]:<4}{r[1]:<16}{(r[2] or ''):<16}{r[3]:<10}"
              f"{'是' if r[4] else '否':<6}{r[5] or '-'}")
    n_sess = conn.execute("SELECT COUNT(*) FROM auth_session").fetchone()[0]
    print(f"\n  当前活跃会话: {n_sess} 条")


def main() -> int:
    ap = argparse.ArgumentParser(description="离线重置账号口令（并吊销该账号会话）")
    ap.add_argument("--user", default="root", help="用户名（默认 root）")
    ap.add_argument("--password", default=None,
                    help="新口令；不传则交互式输入（推荐，不进 shell 历史）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help=f"鉴权库路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--list", action="store_true", help="只列出账号与会话数，不修改")
    ap.add_argument("--keep-sessions", action="store_true",
                    help="保留该账号已有会话（默认吊销；只在明确知道后果时使用）")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[ERROR] 鉴权库不存在: {db_path}")
        print("        提示：后端首次启动会自动创建；或检查 AUTH_DB_PATH 配置。")
        return 2

    if args.list:
        conn = sqlite3.connect(str(db_path))
        _list_users(conn)
        conn.close()
        return 0

    S = _load_security()

    # 1) 先定位账号（只读）——放在备份之前，避免口令不合规时留下无用备份
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM auth_user WHERE username=?", (args.user,)).fetchone()
    if not row:
        names = [r[0] for r in conn.execute("SELECT username FROM auth_user")]
        print(f"[ERROR] 账号不存在: {args.user!r}；现有账号: {names}")
        conn.close()
        return 2
    print(f"[1/4] 目标账号: {row['username']}（{row['display_name'] or '-'}，角色 {row['role']}）")

    # 2) 取口令 + 校验策略（与在线路径同一个函数）
    password = args.password
    if password is None:
        password = getpass.getpass("  请输入新口令（不回显）: ")
        again = getpass.getpass("  再输一次确认: ")
        if password != again:
            print("[ERROR] 两次输入不一致")
            conn.close()
            return 2
    try:
        S._validate_password(password, row["username"])          # noqa: SLF001（与在线同一套策略）
    except ValueError as e:
        print(f"[ERROR] 口令不符合策略: {e}（未做任何修改）")
        conn.close()
        return 2
    new_hash = S.hash_password(password)

    # 3) 备份（此时已确认要动手）
    if not args.no_backup:
        dest = _backup(db_path)
        print(f"[2/4] 已备份: {dest.name}")
    else:
        print("[2/4] 跳过备份（--no-backup）")

    # 4) 写库 + 吊销会话
    conn.execute("UPDATE auth_user SET password_hash=? WHERE id=?", (new_hash, row["id"]))
    revoked = 0
    if args.keep_sessions:
        print("[3/4] 按要求**保留**已有会话（--keep-sessions）")
    else:
        cur = conn.execute("DELETE FROM auth_session WHERE username=?", (row["username"],))
        revoked = cur.rowcount
        print(f"[3/4] 已吊销该账号会话: {revoked} 条")
    conn.commit()

    left = conn.execute("SELECT COUNT(*) FROM auth_session WHERE username=?",
                        (row["username"],)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM auth_session").fetchone()[0]
    conn.close()

    # 自证：用刚写进去的哈希反验一次（避免"写了但登录不了"）
    conn = sqlite3.connect(str(db_path))
    stored = conn.execute("SELECT password_hash FROM auth_user WHERE id=?", (row["id"],)).fetchone()[0]
    conn.close()
    if not S.verify_password(password, stored):
        print("[ERROR] 自检失败：写入的哈希无法通过校验（未改动会话，请检查）")
        return 3

    print(f"[4/4] 自检通过（哈希可校验）")
    if args.keep_sessions:
        tail = "（会话已保留 —— 未踢下线）"
    else:
        tail = f"，该账号剩余会话 {left} 条"
    print(f"\n完成：{row['username']} 口令已重置{tail}；全库会话 {total} 条")
    print("\n下一步：docker compose start backend，然后用新口令登录。")
    if not args.no_backup:
        print("确认能登录后，可删除同目录下的 .bak-* 备份。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
