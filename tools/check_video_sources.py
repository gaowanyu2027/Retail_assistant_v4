"""视频源体检 —— 把配置里的每台摄像头都真开一遍，报告能不能出画面。

为什么要有这个：视频源打不开的原因很多（设备不可达、依赖缺失、容器看不到物理设备、
路径不在白名单），而出问题时界面只给一个状态码。本工具直接把每条链路跑一遍，
把"哪一台、失败在哪一步、报什么码"一次列清楚。

用法（在项目根目录执行）：

    # 用登录口令（走真实登录接口，推荐）
    python tools/check_video_sources.py --user root --password '你的密码'

    # 已有会话令牌
    python tools/check_video_sources.py --token <令牌>

    # 本机开发便利：直接给已有的 root 账号签一个临时会话（跑完自动吊销）
    python tools/check_video_sources.py --mint-session

可选：--seconds 8（每台最多观察多少秒）、--url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import websockets  # noqa: E402


def _login(base: str, user: str, password: str) -> str:
    r = httpx.post(f"{base}/api/auth/login", json={"username": user, "password": password}, timeout=20)
    if r.status_code != 200:
        raise SystemExit(f"登录失败 HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    return data.get("token") or data.get("access_token") or ""


def _mint_session() -> str:
    """本机开发便利：给已有的 root 账号签一个临时会话（不进密码）。"""
    import api.security as S
    import sqlite3
    conn = sqlite3.connect(S.AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, username, role FROM auth_user "
                       "WHERE role='root' AND enabled=1 ORDER BY id LIMIT 1").fetchone()
    conn.close()
    if not row:
        raise SystemExit("鉴权库里没有启用的 root 账号，请改用 --user/--password")
    sess = S.create_session(row["id"], row["username"], row["role"], ip="127.0.0.1",
                            user_agent="check_video_sources")
    return sess["token"] if isinstance(sess, dict) else sess


async def _try_open(base: str, token: str, source: dict, seconds: float) -> dict:
    """开一次源并观察：返回 {frames, first_frame_s, status[], ok}"""
    import time
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/api/ws/stream"
    out = {"frames": 0, "bytes": 0, "first_frame_s": None, "states": [], "error": None}
    t0 = time.time()
    try:
        async with websockets.connect(ws_url, additional_headers={"Authorization": f"Bearer {token}"},
                                      max_size=None, open_timeout=15) as ws:
            await ws.send(json.dumps({"action": "open_source", "source": source}, ensure_ascii=False))
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=max(0.3, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, (bytes, bytearray)):
                    out["frames"] += 1
                    out["bytes"] += len(msg)
                    if out["first_frame_s"] is None:
                        out["first_frame_s"] = round(time.time() - t0, 2)
                    continue
                try:
                    m = json.loads(msg)
                except Exception:
                    continue
                if m.get("type") == "source_status":
                    out["states"].append(m.get("state"))
                    if m.get("state") == "error":
                        out["error"] = f"{m.get('code')}: {m.get('message')}"
                        break
                    if m.get("state") == "finished":
                        break
            await ws.send(json.dumps({"action": "stop"}))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["ok"] = out["frames"] > 0
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="把配置里的每台摄像头真开一遍，报告能否出画面")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--token")
    ap.add_argument("--mint-session", action="store_true")
    ap.add_argument("--seconds", type=float, default=25.0,
                    help="每台最多观察秒数。默认 25 秒是**故意留长**的："
                         "不可达的 RTSP 要等约 20 秒才会报 source_open_failed，"
                         "窗口太短只会看到 opening，得不出结论")
    ap.add_argument("--rtsp", help="额外单独测一个 RTSP 地址")
    args = ap.parse_args()

    minted = None
    token = args.token
    if not token:
        if args.mint_session:
            token = minted = _mint_session()
            print("[会话] 已为本机 root 账号签临时会话（结束自动吊销）")
        elif args.user and args.password:
            token = _login(args.url, args.user, args.password)
            print("[会话] 登录成功")
        else:
            raise SystemExit("请提供 --user/--password 或 --token 或 --mint-session")
    h = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=20) as c:
        caps = (await c.get(f"{args.url}/api/video/sources", headers=h)).json()
        fams = (await c.get(f"{args.url}/api/cameras", headers=h)).json()
        cams = fams.get("cameras") or fams.get("data") or []

    print("\n=== 能力（服务端自述）===")
    for k in caps.get("kinds", []):
        flag = "可用" if k.get("available") else "不可用"
        why = f"  —— {k.get('reason')}" if k.get("reason") else ""
        print(f"  {k.get('kind'):<8} {flag}{why}")

    print(f"\n=== 逐台打开（每台最多观察 {args.seconds:g} 秒）===")
    bad = 0
    for cam in cams:
        res = await _try_open(args.url, token, {"kind": "camera", "id": cam["id"]}, args.seconds)
        mark = "✅" if res["ok"] else "❌"
        if not res["ok"]:
            bad += 1
        detail = (f"{res['frames']} 帧 / {res['bytes']} 字节，首帧 {res['first_frame_s']}s"
                  if res["ok"] else f"状态流转 {res['states'] or '-'}；{res['error'] or '未见画面'}")
        print(f"  {mark} {cam['id']:<14} {str(cam.get('source'))[:44]:<46} {detail}")

    if args.rtsp:
        res = await _try_open(args.url, token, {"kind": "rtsp", "url": args.rtsp}, args.seconds)
        print(f"\n=== 单独测 RTSP ===")
        print(f"  {'✅' if res['ok'] else '❌'} {args.rtsp} -> "
              f"{res['frames']} 帧；{res['error'] or res['states']}")

    if minted:
        import api.security as S
        S.revoke_session(minted)
        print("\n[清理] 临时会话已吊销")

    print(f"\n结论：{len(cams) - bad}/{len(cams)} 台能出画面"
          + ("（不可用的那些请看上面的错误码/原因）" if bad else ""))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
