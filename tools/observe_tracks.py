"""只读旁观：数"实际出现了多少个不同 track_id"，与"总访客数"对照（一次性工具）。

用法（不占用「本机摄像头」模式、不切换视频源，只是**接上去读**）：
    python tools/observe_tracks.py --seconds 30
    python tools/observe_tracks.py --mint-session --seconds 30   # 本机开发便利

判读：
  最大同时活跃轨迹 = N，但一段时间内出现过的不同 track_id 远多于 N
      -> **跟踪器 ID 切换**（同一人被反复赋新 ID）→ 访客数被算成人次
  不同 track_id 很少、访客数却涨得快
      -> **代码去重问题**
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import websockets  # noqa: E402


def _mint_session() -> str:
    import api.security as S
    conn = sqlite3.connect(S.AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, username, role FROM auth_user "
                       "WHERE role='root' AND enabled=1 ORDER BY id LIMIT 1").fetchone()
    conn.close()
    if not row:
        raise SystemExit("鉴权库里没有启用的 root 账号")
    s = S.create_session(row["id"], row["username"], row["role"], ip="127.0.0.1",
                         user_agent="observe_tracks")
    return s["token"] if isinstance(s, dict) else s


async def main() -> int:
    ap = argparse.ArgumentParser(description="旁观统计 track_id 数量与访客数")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--mint-session", action="store_true")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--token")
    args = ap.parse_args()

    token = args.token
    if not token and args.mint_session:
        token = _mint_session()
    if not token and args.user:
        r = httpx.post(f"{args.url}/api/auth/login",
                       json={"username": args.user, "password": args.password}, timeout=20)
        token = (r.json().get("token") or "")
    if not token:
        raise SystemExit("请提供 --mint-session 或 --user/--password 或 --token")
    h = {"Authorization": f"Bearer {token}"}

    ws_url = args.url.replace("https://", "wss://").replace("http://", "ws://") + "/api/ws/stream"

    def snapshot():
        try:
            d = httpx.get(f"{args.url}/api/reports/popularity", headers=h, timeout=20).json()
            return d.get("total_visitors"), d.get("total_visits")
        except Exception as e:
            return f"查询失败({type(e).__name__})", None

    before = snapshot()
    print(f"[开始] 旁观 {args.seconds:g} 秒（只读，不改视频源）｜访客数基线: 唯一={before[0]} 到访={before[1]}")

    ids: Counter = Counter()
    per_sample_active = []
    meta_msgs = binary = 0
    t0 = time.time()

    async with websockets.connect(ws_url, additional_headers=h, max_size=None,
                                  open_timeout=15) as ws:
        while time.time() - t0 < args.seconds:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, (bytes, bytearray)):
                binary += 1
                continue
            try:
                m = json.loads(msg)
            except Exception:
                continue
            if m.get("type") != "frame":
                continue
            meta_msgs += 1
            tr = m.get("tracks") or []
            per_sample_active.append(len(tr))
            for t in tr:
                tid = t.get("track_id")
                if tid is not None:
                    ids[tid] += 1

    after = snapshot()
    el = time.time() - t0
    print(f"[结束] 旁观 {el:.1f}s｜收到元数据帧 {meta_msgs} 条、二进制帧 {binary} 个")
    print(f"  最大同时活跃轨迹: {max(per_sample_active) if per_sample_active else 0}")
    print(f"  出现过的**不同 track_id**: {len(ids)}  -> {sorted(ids)[:20]}{' …' if len(ids) > 20 else ''}")
    if ids:
        print(f"  各 id 被观测到的次数（前 10）: {ids.most_common(10)}")
    print(f"  访客数: 唯一 {before[0]} -> {after[0]}（增量 {_delta(before[0], after[0])}）"
          f"｜区域到访 {before[1]} -> {after[1]}（增量 {_delta(before[1], after[1])}）")

    mx = max(per_sample_active) if per_sample_active else 0
    if mx and len(ids) > mx * 2:
        print(f"\n[判读] 同时最多 {mx} 个人，却出现 {len(ids)} 个不同 track_id"
              f"（{len(ids)/mx:.1f} 倍）→ **跟踪器 ID 切换**：同一个人被反复赋新 ID，"
              f"访客数因此被算成\"人次\"。")
    elif ids:
        print(f"\n[判读] 不同 id={len(ids)}、最大同时活跃={mx} —— ID 切换不严重；"
              f"若访客增量明显大于人数，则要看代码去重逻辑。")
    else:
        print("\n[提示] 没收到任何 tracks 元数据 —— 说明当前没有视频源在跑（或没有轨迹）。"
              "请先在界面上打开一个摄像头再看。")
    return 0


def _delta(a, b):
    try:
        return b - a
    except Exception:
        return "?"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
