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


def mode_web(host: str, port: int):
    """Web界面模式 — 启动FastAPI + 前端"""
    try:
        import uvicorn
    except ImportError:
        print("[ERROR] 未安装 uvicorn，请执行: pip install uvicorn")
        sys.exit(1)

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

    args = parser.parse_args()
    ensure_dirs()

    if args.mode == "web":
        mode_web(args.host, args.port)
