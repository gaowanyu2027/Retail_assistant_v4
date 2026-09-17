"""把任意画面**通过接口推入服务端**（容器/远程都适用）。

为什么需要它：容器看不到物理摄像头（Windows 上 Docker 跑在 WSL2，内核没有摄像头设备节点，
也无法把 USB 设备直通进去）。但服务端本来就提供了一条**上行通道**：
`/api/ws/client` 接收二进制图像帧（JPEG/PNG，OpenCV 能解的格式），解码后交给分析管线，
结果再从 `/api/ws/stream` 推回来。所以——**摄像头在哪台机器上不重要，能跑本脚本就行**。

典型用法：

    # ① 把**这台机器**的摄像头推进去（服务器看不到物理设备时最常用）
    python tools/push_camera_frames.py --source 0 --user root --password '你的密码'

    # ② 把一段视频当"实时流"推进去（演示/回放；--loop 循环播）
    python tools/push_camera_frames.py --source data/sources/long_demo.mp4 --loop

    # ③ 把"服务端网络不可达、但本机可达"的网络摄像头转发进去
    python tools/push_camera_frames.py --source 'rtsp://admin:密码@192.168.1.64:554/Streaming/Channels/101'

    # 已有会话令牌时
    python tools/push_camera_frames.py --source 0 --token <令牌>

脚本会**自己验证**是否真的被接收：它同时盯着 `/api/ws/stream` 的 `source_status`
与返回的处理后画面，结束时打印"推了 N 帧 / 服务端处理并回传 M 帧"。
若一直 0 帧，说明服务端没吃到帧（多半是另有一个会话正占用「本机摄像头」模式）。

按 Ctrl+C 停止。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import cv2  # noqa: E402
import httpx  # noqa: E402
import websockets  # noqa: E402


def _login(base: str, user: str, password: str) -> str:
    r = httpx.post(f"{base}/api/auth/login", json={"username": user, "password": password}, timeout=20)
    if r.status_code != 200:
        raise SystemExit(f"登录失败 HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    return d.get("token") or d.get("access_token") or ""


def _open_capture(source: str):
    """source：数字=本机设备号；其余按 路径/URL 处理（OpenCV 会自动区分文件与 RTSP）。"""
    try:
        return cv2.VideoCapture(int(source))
    except ValueError:
        cap = cv2.VideoCapture(source)
        if not cap.isOpened() and not source.startswith(("rtsp://", "rtps://", "http")):
            path = PROJECT_ROOT / source if not Path(source).is_absolute() else Path(source)
            if path.exists():
                cap = cv2.VideoCapture(str(path))
        return cap


def _mint_session() -> str:
    """本机开发便利：给已有的 root 账号签一个临时会话（不进密码）。跑完请自行吊销。"""
    import sqlite3

    import api.security as S
    conn = sqlite3.connect(S.AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, username, role FROM auth_user "
                       "WHERE role='root' AND enabled=1 ORDER BY id LIMIT 1").fetchone()
    conn.close()
    if not row:
        raise SystemExit("鉴权库里没有启用的 root 账号，请改用 --user/--password")
    sess = S.create_session(row["id"], row["username"], row["role"], ip="127.0.0.1",
                            user_agent="push_camera_frames")
    return sess["token"] if isinstance(sess, dict) else sess


async def run(args) -> int:
    base = args.url.rstrip("/")
    token = args.token or (args.mint_session and _mint_session()) or _login(base, args.user, args.password)
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    headers = {"Authorization": f"Bearer {token}"}

    cap = _open_capture(args.source)
    if not cap.isOpened():
        raise SystemExit(f"打不开视频源：{args.source}（本机设备号写 0/1；文件写路径；网络写 rtsp://…）")
    print(f"[源] {args.source}  {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
          f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))} @ {cap.get(cv2.CAP_PROP_FPS):.1f}fps")

    stats = {"pushed": 0, "processed": 0, "first_back_s": None, "states": [], "error": None}
    stop_evt = asyncio.Event()
    t0 = time.time()

    async with websockets.connect(f"{ws_base}/api/ws/stream", additional_headers=headers,
                                  max_size=None, open_timeout=15) as ws_stream:
        await ws_stream.send(json.dumps({"action": "open_source", "source": {"kind": "client"}}))

        async def watch_stream():
            """盯回执与返回画面：证明服务端真的吃到了帧。"""
            while not stop_evt.is_set():
                try:
                    msg = await asyncio.wait_for(ws_stream.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    return
                if isinstance(msg, (bytes, bytearray)):
                    stats["processed"] += 1
                    if stats["first_back_s"] is None:
                        stats["first_back_s"] = round(time.time() - t0, 2)
                    continue
                try:
                    m = json.loads(msg)
                except Exception:
                    continue
                mtype = m.get("type")
                # ⚠ 必须把**所有**控制类消息都暴露出来：服务端的失败既有
                # `source_status{state:error}`（帧/管线层面），也有 `status{status:error}`
                # （会话冲突等）。只盯前者就会漏掉"已被其他会话占用"这种拒绝，
                # 然后误判成"推了帧但没反应"（自己踩过）。
                if mtype == "source_status":
                    st = m.get("state")
                    stats["states"].append(st)
                    if st == "opening":
                        print("[服务端] 正在打开浏览器采帧模式…")
                    elif st == "running":
                        print("[服务端] 已开始输出画面（说明帧被成功接收并处理）")
                    elif st == "error":
                        stats["error"] = f"{m.get('code')}: {m.get('message')}"
                        print(f"[服务端] 拒绝/失败：{stats['error']}")
                        stop_evt.set()
                        return
                elif mtype in ("status", "error"):
                    st = m.get("status") or "error"
                    txt = (m.get("message") or "")[:200]
                    stats["states"].append(f"{mtype}:{st}")
                    print(f"[服务端] {mtype}: {st} {txt}")
                    if st == "error":
                        stats["error"] = txt
                        stop_evt.set()
                        return

        watcher = asyncio.create_task(watch_stream())

        async with websockets.connect(f"{ws_base}/api/ws/client", additional_headers=headers,
                                      max_size=None, open_timeout=15) as ws_client:
            print(f"[上行] 已连接 {ws_base}/api/ws/client，开始推帧（Ctrl+C 停止）")
            interval = 1.0 / max(args.fps, 1)
            while not stop_evt.is_set():
                loop_start = time.time()
                ok, frame = cap.read()
                if not ok:
                    if args.loop and not str(args.source).isdigit():
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    print("[源] 读完了")
                    break
                if args.width and args.height:
                    frame = cv2.resize(frame, (args.width, args.height), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
                if ok:
                    await ws_client.send(buf.tobytes())
                    stats["pushed"] += 1
                if stats["pushed"] % max(int(args.fps * 3), 10) == 0 and stats["pushed"]:
                    print(f"  已推 {stats['pushed']} 帧｜服务端回传 {stats['processed']} 帧"
                          f"｜首帧回传 {stats['first_back_s']}s")
                if args.max_seconds and time.time() - t0 >= args.max_seconds:
                    print(f"[停止] 达到 --max-seconds={args.max_seconds}")
                    break
                await asyncio.sleep(max(0.0, interval - (time.time() - loop_start)))

            stop_evt.set()
            try:
                await ws_stream.send(json.dumps({"action": "stop"}))
            except Exception:
                pass
            watcher.cancel()

    cap.release()
    print(f"\n[汇总] 推送 {stats['pushed']} 帧｜服务端处理并回传 {stats['processed']} 帧"
          f"｜首帧回传 {stats['first_back_s']}s｜状态流转 {stats['states']}")
    if stats["processed"] == 0:
        print("⚠ 服务端没有回传任何画面：多半是**已有另一个会话占用「本机摄像头」模式**"
              "（该模式按连接独占），或帧格式不被识别（用 JPEG/PNG）。")
    return 0 if stats["processed"] > 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="把任意画面通过 /api/ws/client 推入服务端")
    ap.add_argument("--source", required=True,
                    help="0/1=本机设备号；文件路径；或 rtsp://… 地址")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password")
    ap.add_argument("--token")
    ap.add_argument("--mint-session", action="store_true",
                    help="本机开发便利：直接给已有 root 账号签临时会话（跑完请按需吊销）")
    ap.add_argument("--fps", type=float, default=15.0)
    ap.add_argument("--quality", type=int, default=60, help="JPEG 质量 1-100（越小越省带宽）")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--loop", action="store_true", help="文件播完后从头再来")
    ap.add_argument("--max-seconds", type=float, default=0, help="推送多久后自动停止（0=不限）")
    args = ap.parse_args()
    if not args.token and not args.password and not args.mint_session:
        raise SystemExit("请提供 --password、--token 或 --mint-session")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[中断] 已停止推送")
        return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
