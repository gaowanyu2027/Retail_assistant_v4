"""
FastAPI 主入口 — 整合零售视频分析 + 门店人脸表情分析双系统

启动方式:
    python run.py
    或
    python -m uvicorn api.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, UploadFile, File as FastAPIFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import API_HOST, API_PORT, ensure_dirs


# ==================== 缓存清理线程 ====================

def _cleanup_worker():
    """后台线程：定期清理过期视频与数据库旧记录"""
    from config.settings import (
        CACHE_CLEAN_INTERVAL_SECONDS, CACHE_MAX_AGE_HOURS,
        DB_RECORD_KEEP_DAYS, VIDEO_OUTPUT_DIR,
    )
    from database import cleanup_old_records

    while True:
        time.sleep(CACHE_CLEAN_INTERVAL_SECONDS)
        try:
            now = datetime.now()
            cutoff = now - timedelta(hours=CACHE_MAX_AGE_HOURS)
            removed = 0
            if os.path.exists(VIDEO_OUTPUT_DIR):
                for fname in os.listdir(VIDEO_OUTPUT_DIR):
                    fpath = os.path.join(VIDEO_OUTPUT_DIR, fname)
                    if os.path.isfile(fpath):
                        mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                        if mtime < cutoff:
                            try:
                                os.remove(fpath)
                                removed += 1
                            except Exception:
                                pass
            deleted_db = cleanup_old_records(DB_RECORD_KEEP_DAYS)
            if removed or deleted_db:
                print(f"[清理] 删除过期视频 {removed} 个，清理数据库记录 {deleted_db} 条")
            # Agent 存储清理：检查点/长期记忆/日志（幂等，失败仅告警）
            try:
                from agents.memory_cleanup import cleanup_agent_storage
                cleanup_agent_storage()
            except Exception as e:
                print(f"[清理] Agent 存储清理异常: {e}")
        except Exception as e:
            print(f"[清理] 异常: {e}")


# ==================== 应用生命周期 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 60)
    print("  🏪 智能零售分析系统 (零售分析 + 表情分析) — 启动中...")
    print("=" * 60)

    ensure_dirs()

    # 初始化表情数据库
    try:
        from database import init_db
        init_db()
        from database import _mysql_enabled
        if _mysql_enabled():
            print("[OK] 数据写入: MySQL Retail_assistant")
        else:
            print("[WARN] 未检测到 mysql_root，当前回退到 SQLite")
    except Exception as e:
        print(f"[WARN] 表情数据库初始化失败: {e}")

    # 重建查询历史向量索引（后台线程执行，不阻塞服务就绪——
    # 全量 embed 655+ 条记录串行调用 Ollama 需数十秒，同步执行会导致启动卡死）
    def _background_reindex():
        try:
            import vector_memory
            count = vector_memory.reindex_all()
            print(f"[OK] 查询历史向量索引完成: {count} 条")
        except Exception as e:
            print(f"[WARN] 查询历史向量索引失败: {e}")

    threading.Thread(target=_background_reindex, daemon=True).start()
    print("[OK] 查询历史向量索引: 后台重建中（不阻塞启动）")

    # 预热CV引擎
    try:
        from api.dependencies import get_detector, get_tracker, get_roi_manager
        detector = get_detector()
        tracker = get_tracker()
        roi_mgr = get_roi_manager()
        app.state.ready = True
        print(f"[OK] CV引擎初始化完成 (设备: {detector.device}, ROI区域: {len(roi_mgr.zones)}个)")
    except Exception as e:
        print(f"[WARN] CV引擎初始化警告: {e}")
        app.state.ready = False

    # 启动缓存清理线程
    t_cleanup = threading.Thread(target=_cleanup_worker, daemon=True)
    t_cleanup.start()

    # 启动定期热度汇报（主动汇报 Agent：LLM 摘要 + 异常突增推送）
    try:
        from api.routes.report import set_report_loop
        import asyncio as _asyncio
        set_report_loop(_asyncio.get_running_loop())
        from scheduled_tasks import start_scheduled_tasks
        start_scheduled_tasks()
        print("[OK] 主动汇报 Agent 已启动（周期汇报 + 异常突增推送）")
    except Exception as e:
        print(f"[WARN] 定期热度汇报启动失败: {e}")

    print(f"[OK] API地址: http://{API_HOST}:{API_PORT}")
    print(f"[OK] 仪表盘: http://localhost:{API_PORT}")
    print(f"[OK] API文档: http://localhost:{API_PORT}/docs")
    print("=" * 60)

    yield

    print("[STOP] 系统关闭中...")
    app.state.ready = False
    # 主动断开浏览器 WebSocket + 停止视频处理线程，避免 uvicorn 无限等待优雅关闭
    try:
        from api.routes.stream import shutdown_websockets, _stop_internal
        await shutdown_websockets()
        await asyncio.to_thread(_stop_internal)
    except Exception as e:
        print(f"[STOP] 清理异常: {e}")


# ==================== FastAPI App ====================

app = FastAPI(
    title="智能零售分析系统",
    description="整合零售视频分析(货架摄像头) + 门店人脸表情分析(出入口摄像头)双系统",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_static_files(request, call_next):
    """开发阶段避免浏览器缓存旧的 CSS/JS 导致页面看不到最新功能。"""
    response = await call_next(request)
    if request.url.path.startswith(("/css/", "/js/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


# 注册路由
from api.routes.query import router as query_router
from api.routes.report import router as report_router
from api.routes.stream import router as stream_router
from api.routes.emotion_camera import router as emotion_camera_router
from api.routes.voice import router as voice_router
from api.routes.asr import router as asr_router
from api.routes.tts import router as tts_router
from api.routes.chat import router as chat_router
from api.routes.analytics import router as analytics_router

app.include_router(query_router, prefix="/api")
app.include_router(report_router, prefix="/api")
app.include_router(stream_router, prefix="/api")
app.include_router(emotion_camera_router, prefix="/api")
app.include_router(voice_router, prefix="/api")
app.include_router(asr_router, prefix="/api")
app.include_router(tts_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")

# ==================== 前端入口选择（Vue 版优先，异常时自动回退原生 JS 版） ====================

NATIVE_FRONTEND_DIR = PROJECT_ROOT / "frontend"
VUE_DIST_DIR = PROJECT_ROOT / "frontend-vue" / "dist"

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

# Vue 页面加载失败时注入的回退脚本：资源加载失败 / 挂载超时 / 未处理异常
# → 上报后端（终端打印日志）→ 跳转到 /?vue=0 的原生 JS 版
VUE_FALLBACK_SCRIPT = """
<script>
(function () {
  var done = false;
  function fallback(reason) {
    if (done) return;
    done = true;
    try {
      fetch('/api/frontend/fallback?reason=' + encodeURIComponent(reason), { method: 'POST' });
    } catch (e) {}
    location.replace('/?vue=0');
  }
  window.addEventListener('error', function (e) {
    var src = (e.target && (e.target.src || e.target.href)) || '';
    if (src && src.indexOf('/assets/') !== -1) {
      fallback('资源加载失败: ' + src);
    }
  }, true);
  window.addEventListener('unhandledrejection', function () {
    var app = document.getElementById('app');
    if (!app || app.childElementCount === 0) fallback('未处理的Promise异常');
  });
  setTimeout(function () {
    var app = document.getElementById('app');
    if (!app || app.childElementCount === 0) fallback('Vue 挂载超时');
  }, 8000);
})();
</script>
"""

_vue_ok: bool = False


def _collect_local_refs(index_html: str) -> list[str]:
    """提取 index.html 中引用的本地静态资源（过滤协议/锚点/查询串）。"""
    refs: list[str] = []
    for m in re.finditer(r'(?:src|href)="([^"]+)"', index_html):
        ref = m.group(1)
        if not ref or ref.startswith(("http://", "https://", "data:", "//")):
            continue
        ref = ref.split("#")[0].split("?")[0]
        refs.append(ref)
    return refs


def _vue_dist_available() -> tuple[bool, list[str]]:
    """检查 Vue 构建产物是否完整。返回 (是否可用, 缺失文件列表)。"""
    index_path = VUE_DIST_DIR / "index.html"
    if not index_path.exists():
        return False, ["index.html"]
    try:
        html = index_path.read_text(encoding="utf-8")
    except Exception as e:
        return False, [f"index.html 读取失败: {e}"]

    missing: list[str] = []
    for ref in _collect_local_refs(html):
        ref_clean = ref.lstrip("/")
        if not (VUE_DIST_DIR / ref_clean).exists():
            missing.append(ref_clean)
    return (len(missing) == 0), missing


def _serve_native():
    """返回原生 JS 版前端页面。"""
    index_path = NATIVE_FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path), headers=dict(NO_CACHE_HEADERS))
    return {"message": "前端文件未找到，请访问 /docs 查看API文档"}


# 启动时检查 Vue 构建产物，决定默认前端并打印日志
_vue_ok, _vue_missing = _vue_dist_available()
if _vue_ok:
    print("[Frontend] Vue 版构建产物完整，默认使用 Vue 版 (frontend-vue/dist)")
else:
    print("[Frontend][WARN] Vue 版构建产物不完整，自动回退为原生 JS 版 (frontend/)")
    for _f in _vue_missing:
        print(f"[Frontend][WARN]   缺失: frontend-vue/dist/{_f}")

# 静态文件挂载：Vue 版可用时挂载 dist（含 /assets），否则挂载原生目录
_static_base = VUE_DIST_DIR if _vue_ok else NATIVE_FRONTEND_DIR
_css_dir = _static_base / "css"
if not _css_dir.exists():
    _css_dir = NATIVE_FRONTEND_DIR / "css"
_js_dir = _static_base / "js"
if not _js_dir.exists():
    _js_dir = NATIVE_FRONTEND_DIR / "js"
if _css_dir.exists():
    app.mount("/css", StaticFiles(directory=str(_css_dir)), name="css")
if _js_dir.exists():
    app.mount("/js", StaticFiles(directory=str(_js_dir)), name="js")
if _vue_ok and (VUE_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(VUE_DIST_DIR / "assets")), name="assets")
print(f"[OK] 前端静态文件: {_static_base}")


@app.get("/")
async def serve_index(vue: str = ""):
    """根路径返回前端页面。

    - 默认: Vue 构建版（frontend-vue/dist），并在页面注入加载失败回退脚本
    - ?vue=0: 强制原生 JS 版（frontend/）
    - Vue 构建产物在运行期缺失时，自动回退原生版并在终端打印日志
    """
    global _vue_ok

    if vue == "0":
        if _vue_ok:
            print("[Frontend] 客户端请求回退，返回原生 JS 版页面")
        return _serve_native()

    # 每请求轻量复查：构建产物在运行期被破坏时自动回退
    if _vue_ok:
        _ok, _missing = _vue_dist_available()
        if not _ok:
            _vue_ok = False
            print("[Frontend][WARN] Vue 构建产物在运行期缺失，自动回退为原生 JS 版:")
            for _f in _missing:
                print(f"[Frontend][WARN]   缺失: frontend-vue/dist/{_f}")

    if _vue_ok:
        try:
            html = (VUE_DIST_DIR / "index.html").read_text(encoding="utf-8")
            html = html.replace("</body>", VUE_FALLBACK_SCRIPT + "</body>", 1)
            return HTMLResponse(html, headers=dict(NO_CACHE_HEADERS))
        except Exception as e:
            _vue_ok = False
            print(f"[Frontend][WARN] Vue index 读取失败，自动回退为原生 JS 版: {e}")

    return _serve_native()


@app.post("/api/frontend/fallback")
async def frontend_fallback(reason: str = "未知原因"):
    """浏览器端 Vue 加载失败时上报：终端记录原因，客户端随后跳转原生版。"""
    print(f"[Frontend][WARN] Vue 前端加载失败，客户端已自动回退为原生 JS 版。原因: {reason}")
    return {"status": "ok", "fallback": "native"}


@app.get("/api/health")
async def health():
    """健康检查"""
    import torch
    from api.dependencies import get_uptime_seconds
    return {
        "status": "ok",
        "gpu_available": torch.cuda.is_available(),
        "device": "cuda:0" if torch.cuda.is_available() else "cpu",
        "uptime_seconds": get_uptime_seconds(),
    }


@app.post("/api/videos")
async def upload_video_file(file: UploadFile = FastAPIFile(...)):
    """上传视频文件 — 创建视频资源（POST /api/videos）"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="未选择文件")

    allowed_ext = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv"}
    # 仅保留文件名（去掉路径部分），防止目录穿越
    safe_name = file.filename.replace("\\", "/").split("/")[-1].strip()
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", safe_name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="文件名无效")

    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in allowed_ext:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 '{ext}'，允许: {', '.join(allowed_ext)}")

    videos_dir = Path(__file__).resolve().parent.parent / "data" / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    save_path = videos_dir / safe_name

    # 分块写入，避免大文件一次性读入内存
    total_size = 0
    with open(save_path, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            total_size += len(chunk)

    try:
        import mysql_db
        # 同步 DB 写入丢线程池，避免阻塞事件循环
        await asyncio.to_thread(
            mysql_db.save_video_record,
            filename=safe_name,
            file_path=str(save_path.resolve()),
            file_size=total_size,
            source="upload",
            status="uploaded",
        )
    except Exception as e:
        print(f"[MySQL] 视频记录写入失败: {e}")

    file_size_mb = total_size / (1024 * 1024)

    return {
        "status": "ok",
        "filename": safe_name,
        "path": str(save_path.resolve()),
        "size_mb": round(file_size_mb, 2),
        "message": f"文件已上传，可通过 WebSocket 发送播放指令",
        "ws_action": {
            "action": "start_file",
            "file_path": str(save_path.resolve()),
        },
    }


@app.post("/api/video/upload-file", include_in_schema=False)
async def upload_video_file_legacy(file: UploadFile = FastAPIFile(...)):
    """兼容别名：POST /api/video/upload-file（旧路径）→ 等价于 POST /api/videos"""
    return await upload_video_file(file)


# ==================== ROI配置管理 ====================

@app.get("/api/zones")
async def get_zones():
    from api.dependencies import get_roi_manager
    roi_mgr = get_roi_manager()
    zones_data = {}
    for zid in roi_mgr.zones:
        zones_data[zid] = {
            "zone_id": zid,
            "type": roi_mgr.zone_type.get(zid, "shelf"),
            "label": roi_mgr.zone_label.get(zid, zid),
            "polygon": roi_mgr.zones[zid].tolist(),
        }
    return {"zones": zones_data}


@app.put("/api/zones")
async def update_zone(zone: dict):
    zone_id = zone.get("zone_id")
    polygon = zone.get("polygon")
    if not zone_id or not isinstance(zone_id, str):
        raise HTTPException(status_code=422, detail="缺少有效的 zone_id")
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise HTTPException(status_code=422, detail="polygon 必须为至少 3 个顶点的坐标列表")
    for pt in polygon:
        if (not isinstance(pt, (list, tuple)) or len(pt) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pt)):
            raise HTTPException(
                status_code=422,
                detail="polygon 顶点格式错误，应为 [[x1,y1], [x2,y2], ...]")

    zone_type = zone.get("type", "shelf")
    if zone_type not in ("shelf", "checkout", "exit"):
        raise HTTPException(status_code=422, detail="type 必须为 shelf/checkout/exit")
    label = zone.get("label", zone_id)

    from api.dependencies import get_roi_manager
    roi_mgr = get_roi_manager()
    roi_mgr.add_zone(
        zone_id=zone_id,
        zone_type=zone_type,
        label=label,
        polygon=polygon,
    )
    roi_mgr.save_to_yaml()
    try:
        import mysql_db
        mysql_db.upsert_roi_config(
            zone_id=zone_id,
            zone_type=zone_type,
            zone_label=label,
            polygon=polygon,
            source="server",
        )
    except Exception as e:
        print(f"[MySQL] ROI 配置写入失败: {e}")
    return {"status": "ok", "message": f"区域 {zone_id} 已更新"}


@app.delete("/api/zones/{zone_id}")
async def delete_zone(zone_id: str):
    from api.dependencies import get_roi_manager
    roi_mgr = get_roi_manager()
    if roi_mgr.remove_zone(zone_id):
        roi_mgr.save_to_yaml()
        try:
            import mysql_db
            mysql_db.delete_roi_config(zone_id, source="server")
        except Exception as e:
            print(f"[MySQL] ROI 配置删除失败: {e}")
        return {"status": "ok", "message": f"区域 {zone_id} 已删除"}
    return {"status": "error", "message": f"区域 {zone_id} 不存在"}


# ==================== 零售模式表情端点（资源化） ====================

@app.get("/api/emotions/stats")
async def emotion_stats():
    from api.dependencies import get_emotion_skill
    return get_emotion_skill().get_stats()


@app.get("/api/emotions/trend")
async def emotion_trend():
    from api.dependencies import get_emotion_skill
    return get_emotion_skill().get_trend()


@app.get("/api/emotions/recent")
async def emotion_recent(limit: int = 20):
    from api.dependencies import get_emotion_skill
    return {"records": get_emotion_skill().get_recent(limit)}


# ==================== 兼容别名（旧路径，deprecated） ====================

@app.get("/api/emotion/stats", include_in_schema=False)
async def emotion_stats_legacy():
    """兼容别名：GET /api/emotion/stats（旧路径）"""
    return await emotion_stats()


@app.get("/api/emotion/trend", include_in_schema=False)
async def emotion_trend_legacy():
    """兼容别名：GET /api/emotion/trend（旧路径）"""
    return await emotion_trend()


@app.get("/api/emotion/recent", include_in_schema=False)
async def emotion_recent_legacy(limit: int = 20):
    """兼容别名：GET /api/emotion/recent（旧路径）"""
    return await emotion_recent(limit)


@app.get("/api/cameras")
async def scan_cameras():
    """扫描服务器上可用的摄像头设备"""
    import cv2
    cameras = []
    for i in range(8):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cameras.append({"id": i, "resolution": f"{w}x{h}"})
            cap.release()
    return {"cameras": cameras, "default": 0 if cameras else None}


# ==================== 启动入口 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=API_HOST,
        port=API_PORT,
        reload=True,
        log_level="info",
    )
