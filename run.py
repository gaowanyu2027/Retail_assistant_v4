#!/usr/bin/env python
"""
智能零售分析系统 — 一键启动入口

整合功能:
  1. 零售视频分析（货架摄像头）: YOLO26l行人检测+ByteTrack跟踪+ROI热度+异常行为检测
  2. 门店人脸表情分析（出入口摄像头）: YOLOv8n-face人脸检测+MobileNetV3表情识别+SQLite入库

用法:
    # Web界面模式（默认）
    python run.py

    # 指定端口
    python run.py --port 8080
"""
import argparse
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import API_HOST, API_PORT, ensure_dirs


def probe_existing_service(host: str, port: int, timeout: float = 1.5) -> str | None:
    """探测该端口上是否**已有本项目的服务在响应**；有则返回描述，没有返回 None。

    为什么不能只"试着 bind 一下"（E11 的现场教训）：
    容器版在跑时 Docker 代理监听的是 **IPv6**（`:::8000` / `::1:8000`），
    宿主进程绑 `0.0.0.0:8000`（**IPv4**）**并不冲突** —— 于是浏览器按地址族
    随机落到两份实例上，其中一份还卡住了，用户看到的只是"页面没反应"，极难定位。
    所以这里**主动请求一次** `/api/health`，直接问出"是不是已经有人在那儿服务了"。
    """
    import urllib.request
    hosts = (["127.0.0.1"] if host in ("127.0.0.1", "localhost", "0.0.0.0", "") else [host, "127.0.0.1"])
    for h in hosts:
        try:
            with urllib.request.urlopen(f"http://{h}:{port}/api/health", timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "ignore")
            if '"status"' in body:
                return f"{h}:{port} 已有服务在响应 /api/health -> {body[:80]}"
        except Exception:
            continue
    return None


def port_bindable(host: str, port: int) -> bool:
    """能否在本机绑定该端口（用于识别"被非本项目程序占用"的情况）。

    ⚠ **不要设 SO_REUSEADDR**：Windows 上它允许"抢占式绑定"——
    别人占着 `0.0.0.0:port` 时，`127.0.0.1:port` 仍会绑成功，
    于是这个函数会漏报"端口已被占用"（测试里就抓到了这一点）。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0" if host in ("", "0.0.0.0") else host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def check_port_available(host: str, port: int, allow_conflict: bool = False) -> None:
    """启动前的端口自检：占用时给出**可操作**的提示，而不是让用户面对"页面没反应"。"""
    conflict = probe_existing_service(host, port)
    if conflict:
        print("=" * 60)
        print(f"[!] 端口 {port} 上已经有服务在响应，**不建议再启动一份**：")
        print(f"    {conflict}")
        print("    最常见的两种原因：")
        print("      1) 容器版已经在跑 —— Docker 代理只监听 IPv6，与本机的 IPv4 绑定")
        print("         并不冲突，于是浏览器可能随机落到两份实例上，表现为'页面没反应'")
        print("         → 查看: docker compose ps    停止: docker compose stop backend")
        print("      2) 另一个 run.py / 其他程序占着该端口")
        print(f"         → 查看: netstat -ano | findstr :{port}")
        print("    确实要同时启动（不推荐）时，加 --allow-port-conflict。")
        print("=" * 60)
        if not allow_conflict:
            sys.exit(2)
    elif not port_bindable(host, port):
        # 没有人应答 HTTP，但端口也绑不上：更可能是被别的程序占了（非本项目）
        print(f"[!] 端口 {port} 无法绑定（可能被其他程序占用）。")
        print(f"    查看占用: netstat -ano | findstr :{port}")
        if not allow_conflict:
            sys.exit(2)


def mode_web(host: str, port: int, allow_conflict: bool = False):
    """Web界面模式 — 启动FastAPI + 前端"""
    try:
        import uvicorn
    except ImportError:
        print("[ERROR] 未安装 uvicorn，请执行: pip install uvicorn")
        sys.exit(1)

    # 自检放在**加载模型之前**：快速失败，别让用户等 90 秒才发现端口被占
    check_port_available(host, port, allow_conflict)

    print("=" * 60)
    print("  智能零售分析系统 — Web模式")
    print("  [1] 零售视频分析 (货架摄像头)")
    print("  [2] 门店人脸表情分析 (出入口摄像头)")
    print("=" * 60)
    print(f"  访问地址: http://localhost:{port}")
    print(f"  API文档:  http://localhost:{port}/docs")
    print("=" * 60)
    uvicorn.run(
        "api.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
        timeout_graceful_shutdown=5,  # Ctrl+C 后最多等 5 秒（含 WS 断开/线程停止），避免多次按键才退出
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="智能零售分析系统")
    parser.add_argument("--mode", choices=["web"], default="web",
                        help="运行模式: web(Web界面)")
    parser.add_argument("--host", type=str, default=API_HOST,
                        help=f"服务绑定地址 (默认: {API_HOST})")
    parser.add_argument("--port", type=int, default=API_PORT,
                        help=f"服务端口 (默认: {API_PORT})")
    parser.add_argument("--allow-port-conflict", action="store_true",
                        help="跳过端口占用自检（不推荐：两份实例抢同一端口会让浏览器随机落到其中之一）")

    args = parser.parse_args()
    ensure_dirs()

    if args.mode == "web":
        mode_web(args.host, args.port, allow_conflict=args.allow_port_conflict)
