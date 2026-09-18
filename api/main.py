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

from fastapi import FastAPI, UploadFile, File as FastAPIFile, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import API_HOST, API_PORT, AUTH_CORS_ORIGINS, ensure_dirs

# 权限依赖工厂：`Depends(require_perm(...))` 在**函数定义时**求值，
# 所以必须模块级导入（不能像 get_optional_user 那样在函数内局部导入）。
from api.security import require_perm


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

    # 登录鉴权：初始化鉴权库；无任何账号时自动引导创建 root
    # 注意顺序：必须先 init_auth_db()（建表），否则首次启动 purge/bootstrap 会因缺表失败
    try:
        from api.security import (bootstrap_root, init_auth_db, purge_expired_sessions,
                                  purge_login_attempts, purge_expired_tickets)
        init_auth_db()
        boot = bootstrap_root()
        purge_expired_sessions()
        purge_login_attempts()
        purge_expired_tickets()
        if boot:
            print("=" * 60)
            print("  [首次启动] 已创建平台管理员账号（请立即登录并修改密码）")
            print(f"    用户名: {boot['user']['username']}")
            print(f"    密码  : {boot['password']}")
            if boot.get("generated"):
                print("    （随机生成；可用环境变量 AUTH_ROOT_PASSWORD 指定初始密码）")
            print("=" * 60)
        else:
            print("[OK] 登录鉴权已启用")
    except Exception as e:
        print(f"[WARN] 登录鉴权初始化失败: {e}")

    # 启动销量自动同步（POS 目录投递 → 定时扫描导入）
    try:
        from agents.sales_inbox import start_inbox_worker
        start_inbox_worker()
    except Exception as e:
        print(f"[WARN] 销量自动同步启动失败: {e}")

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

# 鉴权中间件：默认封启（/api/* 一律需登录，白名单除外）。
# 纯 ASGI 实现，同时覆盖 HTTP 与 WebSocket，避免视频/推送通道绕过鉴权。
# 注意注册顺序：先加 Auth、后加 CORS → CORS 位于外层，
# 这样中间件直接返回的 401/429 也会带上 CORS 头，跨域客户端才能读到状态码。
from api.security import AuthMiddleware  # noqa: E402

app.add_middleware(AuthMiddleware)

# CORS：**默认不开启跨域**（浏览器前端由本服务同源提供，不需要 CORS）。
# 此前是 allow_origins=["*"] + allow_credentials=True，实测会被反射为
# 「Access-Control-Allow-Origin: <任意站点> + Allow-Credentials: true」，
# 等于把登录态暴露给任意网站（仅靠 SameSite=Lax 兜底，属潜伏风险）。
# 确需跨域（如 Vite dev server 在别的端口）时，用环境变量显式白名单：
#   AUTH_CORS_ORIGINS=http://localhost:5173,https://your-domain.com
if AUTH_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=AUTH_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    print(f"[CORS] 已开启跨域白名单: {AUTH_CORS_ORIGINS}")
else:
    print("[CORS] 未开启跨域（同源访问；如需跨域请设 AUTH_CORS_ORIGINS）")


@app.middleware("http")
async def security_headers(request, call_next):
    """补齐安全响应头（B7）。

    此前**一个都没有**（实测响应头探针返回 {}），意味着：
      - 可被任意站点用 <iframe> 嵌套本系统（点击劫持）
      - 浏览器可能把上传的 JSON/文本按 HTML 嗅探执行（MIME 混淆）
      - 跳转时会带 Referer 泄露内网地址
      - CSP 缺失，一旦有 XSS 就没有第二道防线

    CSP 的取值说明：
      - `script-src 'self'`：前端产物全部是同源外部脚本（Vite 构建，
        index.html 无内联脚本、全仓无 eval/new Function），所以**不需要** unsafe-inline/eval
      - `style-src 'self' 'unsafe-inline'`：index.html 有内联 <style>（v-cloak），
        Vue 也会注入行内样式
      - `img-src/media-src` 允许 `data:` 与 `blob:`：favicon 是 data:URL，
        视频帧/上传预览走 blob:
      - `connect-src 'self' ws: wss:`：需要 WebSocket（视频流/推送/语音）
      - `frame-ancestors 'none'`：等价于 X-Frame-Options: DENY（现代浏览器以 CSP 为准）

    HSTS **仅在 HTTPS 下发送**——在纯 HTTP 开发环境里发 HSTS 没有意义，
    还可能把 localhost 锁进 HTTPS。
    """
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    # 摄像头/麦克风是本系统的核心能力，必须允许同源使用；其余一律关掉
    h.setdefault("Permissions-Policy",
                 "camera=(self), microphone=(self), geolocation=(), payment=()")
    h.setdefault("Content-Security-Policy",
                 "default-src 'self'; "
                 "script-src 'self'; "
                 "style-src 'self' 'unsafe-inline'; "
                 "img-src 'self' data: blob:; "
                 "media-src 'self' blob:; "
                 "connect-src 'self' ws: wss:; "
                 "font-src 'self' data:; "
                 "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if proto == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


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
from api.routes.maps import router as maps_router
from api.routes.cameras import router as cameras_router
from api.routes.multi_stream import router as multi_stream_router
from api.routes.auth import router as auth_router

app.include_router(query_router, prefix="/api")
app.include_router(report_router, prefix="/api")
app.include_router(stream_router, prefix="/api")
app.include_router(emotion_camera_router, prefix="/api")
app.include_router(voice_router, prefix="/api")
app.include_router(asr_router, prefix="/api")
app.include_router(tts_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")
app.include_router(maps_router, prefix="/api")
app.include_router(cameras_router, prefix="/api")
app.include_router(multi_stream_router, prefix="/api")
app.include_router(auth_router, prefix="/api")

# ==================== 前端入口（Vue 版为唯一浏览器前端，原生 JS 版已移除） ====================

VUE_DIST_DIR = PROJECT_ROOT / "frontend-vue" / "dist"

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

# Vue 页面加载失败时的提示脚本（资源失败/挂载超时 → 提示刷新，不再回退原生版）
VUE_FALLBACK_SCRIPT = """
<script>
(function () {
  function notify(reason) {
    if (window.__dshVueFallback) return;
    window.__dshVueFallback = true;
    try {
      fetch('/api/frontend/fallback?reason=' + encodeURIComponent(reason), { method: 'POST' });
    } catch (e) {}
    var box = document.createElement('div');
    box.style.cssText = 'position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);background:#fff;color:#333;padding:24px;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.2);z-index:9999;text-align:center;font-family:sans-serif';
    box.innerHTML = '<div style="font-size:20px;margin-bottom:8px">前端资源加载异常</div>' +
      '<div style="font-size:14px;color:#666">请刷新页面重试，或检查后端控制台日志</div>';
    document.body.appendChild(box);
  }
  window.addEventListener('error', function (e) {
    var src = (e.target && (e.target.src || e.target.href)) || '';
    if (src && src.indexOf('/assets/') !== -1) notify('资源加载失败: ' + src);
  }, true);
  window.addEventListener('unhandledrejection', function () {
    var app = document.getElementById('app');
    if (!app || app.childElementCount === 0) notify('未处理的Promise异常');
  });
  setTimeout(function () {
    var app = document.getElementById('app');
    if (!app || app.childElementCount === 0) notify('Vue 挂载超时');
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


def _serve_unbuilt():
    """Vue 构建产物缺失时的提示页（不再回退原生 JS 版——该版已移除）。"""
    return HTMLResponse(
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>智能零售分析系统</title></head>"
        "<body style='display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;color:#333'>"
        "<div style='text-align:center'><h2>前端未构建</h2>"
        "<p style='color:#666'>Vue 构建产物缺失/不完整，请执行构建后重启服务。</p>"
        "<p style='font-size:13px;color:#999'>frontend-vue/dist/index.html</p></div>"
        "</body></html>",
        headers=dict(NO_CACHE_HEADERS),
    )


# 启动时检查 Vue 构建产物，决定默认前端并打印日志（Vue 是唯一浏览器前端，无回退）
_vue_ok, _vue_missing = _vue_dist_available()
if _vue_ok:
    print("[Frontend] Vue 版构建产物完整 (frontend-vue/dist)")
else:
    print("[Frontend][WARN] Vue 版构建产物不完整，将显示提示页:")
    for _f in _vue_missing:
        print(f"[Frontend][WARN]   缺失: frontend-vue/dist/{_f}")

# 静态文件挂载：仅 Vue 版（原生 JS 版已移除）
_static_base = VUE_DIST_DIR
_css_dir = _static_base / "css"
_js_dir = _static_base / "js"
if _css_dir.exists():
    app.mount("/css", StaticFiles(directory=str(_css_dir)), name="css")
if _js_dir.exists():
    app.mount("/js", StaticFiles(directory=str(_js_dir)), name="js")
if _vue_ok and (VUE_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(VUE_DIST_DIR / "assets")), name="assets")
print(f"[OK] 前端静态文件: {_static_base}")


@app.get("/")
async def serve_index():
    """根路径返回 Vue 前端页面；Vue 构建产物缺失时显示提示页。"""
    global _vue_ok

    # 每请求轻量复查：构建产物在运行期被破坏时切换为提示页
    if _vue_ok:
        _ok, _missing = _vue_dist_available()
        if not _ok:
            _vue_ok = False
            print("[Frontend][WARN] Vue 构建产物在运行期缺失:")
            for _f in _missing:
                print(f"[Frontend][WARN]   缺失: frontend-vue/dist/{_f}")

    if _vue_ok:
        try:
            html = (VUE_DIST_DIR / "index.html").read_text(encoding="utf-8")
            html = html.replace("</body>", VUE_FALLBACK_SCRIPT + "</body>", 1)
            return HTMLResponse(html, headers=dict(NO_CACHE_HEADERS))
        except Exception as e:
            _vue_ok = False
            print(f"[Frontend][WARN] Vue index 读取失败: {e}")

    return _serve_unbuilt()


@app.post("/api/frontend/fallback")
async def frontend_fallback(reason: str = "未知原因"):
    """浏览器端 Vue 加载失败时上报：终端记录原因，客户端随后跳转原生版。"""
    print(f"[Frontend][WARN] Vue 前端加载失败，客户端已自动回退为原生 JS 版。原因: {reason}")
    return {"status": "ok", "fallback": "native"}


@app.get("/api/health")
async def health(request: Request):
    """健康检查。

    匿名访问只返回最小信息（避免设备型号 / GPU 可用性 / 运行时长等指纹外泄）；
    已登录用户返回完整信息。监控探活只需判断 status 字段。
    """
    from api.dependencies import get_uptime_seconds
    from api.security import get_optional_user

    user = await get_optional_user(request)
    if not user:
        return {"status": "ok"}

    import torch
    return {
        "status": "ok",
        "gpu_available": torch.cuda.is_available(),
        "device": "cuda:0" if torch.cuda.is_available() else "cpu",
        "uptime_seconds": get_uptime_seconds(),
        "user": user.get("username"),
        "role": user.get("role"),
    }


@app.get("/api/health/ready")
async def health_ready():
    """**就绪**探针：真实探测依赖，而不是只看进程活着。

    与 `/api/health` 的分工（两者都要保留）：

    | 端点 | 语义 | 是否查依赖 |
    |---|---|---|
    | `/api/health` | liveness：进程还在跑吗 | **不查**（保持向后兼容） |
    | `/api/health/ready` | readiness：现在能干活吗 | **真去 ping** |

    为什么必须加这个：本项目实际发生过"MySQL 口令没传进容器 → 后端静默回退 SQLite
    → `/api/health` 依然 200 healthy → 编排与看板全都以为正常，但业务数据一条都读不到"。
    liveness 探针**天然发现不了**这类问题。

    判定策略（有意区分"致命"与"降级"，避免探针一抖动就把服务判死）：

    - **MySQL 不可用 → 503**：业务数据读写全废，服务等于不能用
    - Qdrant / Redis 不可用 → 仍 200，但计入 `degraded`：向量召回退化为关键词、
      缓存未命中，主链路（问答 / 报表 / 鉴权）不受影响，不该因此判为不可用
    - CV 引擎未初始化 → 计入 `degraded`（容器内没有摄像头，属预期情况）

    匿名可访问（探针无法携带凭据），但**只返回状态与依赖可用性，
    不返回主机名 / 版本 / 设备等指纹**。
    """
    critical: dict[str, bool] = {}
    degraded: list[str] = []
    detail: dict[str, str] = {}

    # ⚠ 下面两处探测都是**同步阻塞 IO**，而本端点是 `async def`：
    #   直接调用会占住事件循环。实测（E13）：Qdrant 不可用时，容器健康检查
    #   （compose 里每 30s 打一次本端点）会让**每次**探测阻塞事件循环约 4 秒 ——
    #   表现为视频帧停顿 3.96s、同时刻 /api/health 尖峰 3.6s，且严格每 34s 复现一次。
    #   所以两个探测一律 `to_thread`，并**并发**执行（总耗时取两者较大值而非相加）。
    def _probe_mysql() -> tuple[bool, str]:
        """致命依赖：真执行一次查询（不用 mysql_available()——它只看环境变量，
        正是当初"假绿灯"的根源）。"""
        try:
            import mysql_db
            conn = mysql_db.get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                return True, ""
            finally:
                conn.close()
        except Exception as e:
            # 只给异常类型，不回显主机名/凭据等细节
            return False, type(e).__name__

    def _probe_qdrant() -> tuple[bool, str]:
        """非致命依赖。返回 (是否降级, detail)。嵌入式模式（未配 QDRANT_URL）不算降级。

        ⚠ 先看**熔断状态**：向量层已熔断（说明确实连不上）时直接报告降级，
        不再发这次 3 秒的探测请求 —— 既省时间，也避免探针自己成为慢端点。
        """
        try:
            import vector_memory
            if vector_memory.breaker_open():
                return True, f"breaker_open({vector_memory.breaker_remaining():.0f}s)"
            qurl = (getattr(vector_memory, "QDRANT_URL", "") or "").rstrip("/")
            if not qurl:
                return False, "embedded"
            import httpx
            r = httpx.get(f"{qurl}/collections", timeout=3.0)
            if r.status_code >= 400:
                return True, f"HTTP {r.status_code}"
            return False, ""
        except Exception as e:
            return True, type(e).__name__

    (mysql_ok, mysql_detail), (qdrant_bad, qdrant_detail) = await asyncio.gather(
        asyncio.to_thread(_probe_mysql),
        asyncio.to_thread(_probe_qdrant),
    )
    critical["mysql"] = mysql_ok
    if mysql_detail:
        detail["mysql"] = mysql_detail
    if qdrant_bad:
        degraded.append("qdrant")
    if qdrant_detail:
        detail["qdrant"] = qdrant_detail

    # ③ Redis：非致命。代码当前尚未读写 Redis（缓存层待接入），
    #    因此**不做探测**——探一个没人用的依赖只会制造噪音。
    #    接入缓存后在此补探测并计入 degraded。

    # ④ CV 引擎：初始化失败只降级。容器内没有摄像头，本就预期如此。
    if getattr(app.state, "ready", None) is False:
        degraded.append("cv_engine")
        detail["cv_engine"] = "not_initialized"

    if not critical.get("mysql"):
        body = {"status": "unavailable", "critical": critical,
                "degraded": degraded, "detail": detail}
        return JSONResponse(status_code=503, content=body)

    body = {"status": "degraded" if degraded else "ok",
            "critical": critical, "degraded": degraded, "detail": detail}
    return body


@app.get("/api/metrics/llm")
async def llm_usage_metrics(_admin: dict = Depends(require_perm("system:manage"))):
    """**LLM 调用指标**（用量 / 延迟 / 失败率 / 按用途归因 / 可选成本估算）。

    为什么要有它：Agent 项目的"慢"和"贵"都藏在 LLM 调用里。本项目实测过一个典型成本问题 ——
    定时汇报**固定 10 分钟一轮 = 144 次/天/店**（台账 F5），但当时**没有任何数据能说明它花了多少**。

    实现方式（关键）：全项目 9 处 LLM 调用，但**只有一个创建入口** `create_llm()`，
    所以指标用 LangChain 的 callback 挂在**创建处**一次覆盖全部 —— 而不是在每个调用点插代码
    （那样漏一处，成本数字就是错的）。

    `tag` 区分用途：`answer`=回答用户 / `report`=定时汇报 / `summary`=会话摘要 / `title`=标题生成。

    ⚠ 诚实边界：
    - `prompt_tokens/completion_tokens` 取自服务端返回的真实用量；**取不到时只计次数不计 token**
      （`calls_without_usage` 会明说有多少次），不猜。
    - `cost_estimate` **只有配置了单价才算**（`LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK`），
      默认 `None` —— 价格会变，写死一个数就是错的。
    - 延迟分位只统计**最近 500 次**（有界窗口），不是历史全量。
    """
    from agents.llm_metrics import get_llm_metrics
    from config.settings import LLM_MODEL, LLM_PRICE_IN_PER_MTOK, LLM_PRICE_OUT_PER_MTOK
    import time as _t

    snap = get_llm_metrics().snapshot(
        price_in_per_mtok=LLM_PRICE_IN_PER_MTOK,
        price_out_per_mtok=LLM_PRICE_OUT_PER_MTOK,
    )
    snap["model"] = LLM_MODEL
    snap["server_time"] = _t.strftime("%Y-%m-%d %H:%M:%S")
    return snap


@app.post("/api/videos")
async def upload_video_file(file: UploadFile = FastAPIFile(...),
                            _uploader: dict = Depends(require_perm("system:manage"))):
    """上传视频文件 — 创建视频资源（POST /api/videos）

    ⚠ 台账 B11 修的就是这个端点，四道约束（缺一不可）：

    1. **权限**：上传会造出**可播放的视频源**，与"设备注册/采集启停"同级 →
       要求 `system:manage`（此前**没有任何权限校验**，任意已登录账号都能传）；
    2. **大小上限** `VIDEO_UPLOAD_MAX_MB`：边写边计数，超限立刻停并删掉半成品
       （原来是无限流写入，把磁盘写满即可让整个服务挂掉）；
    3. **内容校验**：除了扩展名白名单，还按**文件头魔数**判断是不是真的视频容器
       （扩展名随便改，魔数改不了）；
    4. **同名策略**：默认**自动改名**（`demo.mp4` → `demo_1.mp4`），
       不再静默覆盖已有文件（原来同名上传会把演示视频替换掉，事后无从察觉）。
    """
    from video_sources import (
        SourceError, detect_video_container, resolve_name_conflict, safe_upload_name,
    )
    from config.settings import (
        VIDEO_UPLOAD_ALLOWED_EXT, VIDEO_UPLOAD_MAX_MB, VIDEO_UPLOAD_NAME_CONFLICT,
        VIDEO_UPLOAD_VERIFY_MAGIC,
    )

    if not file.filename:
        raise HTTPException(status_code=400, detail="未选择文件")

    try:
        safe_name = safe_upload_name(file.filename, VIDEO_UPLOAD_ALLOWED_EXT)
    except SourceError as e:
        raise HTTPException(status_code=400, detail=f"{e.message}（{e.code}）")

    videos_dir = Path(__file__).resolve().parent.parent / "data" / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    try:
        save_path = resolve_name_conflict(videos_dir, safe_name, VIDEO_UPLOAD_NAME_CONFLICT)
    except SourceError as e:
        raise HTTPException(status_code=409, detail=f"{e.message}（{e.code}）")

    max_bytes = max(1, VIDEO_UPLOAD_MAX_MB) * 1024 * 1024
    total_size = 0
    head = b""
    try:
        # 分块写入，避免大文件一次性读入内存
        with open(save_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                if not head:
                    head = chunk[:64]
                total_size += len(chunk)
                if total_size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(f"文件超过大小上限 {VIDEO_UPLOAD_MAX_MB} MB"
                                f"（已写入 {total_size / 1048576:.1f} MB）。"
                                f"如确需更大文件，请调大 VIDEO_UPLOAD_MAX_MB。"),
                    )
                f.write(chunk)
    except HTTPException:
        save_path.unlink(missing_ok=True)      # 别留半成品文件
        raise
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写入失败：{type(e).__name__}")

    if not total_size:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="上传内容为空")

    if VIDEO_UPLOAD_VERIFY_MAGIC:
        container = detect_video_container(head)
        if container is None:
            save_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=415,
                detail=("文件内容不是可识别的视频容器（已按扩展名放行的文件也要过魔数校验）。"
                        "确认是正常视频仍被拒时可置 VIDEO_UPLOAD_VERIFY_MAGIC=0。"),
            )
        container_name = container
    else:
        container_name = "unchecked"

    try:
        import mysql_db
        # 同步 DB 写入丢线程池，避免阻塞事件循环
        await asyncio.to_thread(
            mysql_db.save_video_record,
            filename=save_path.name,
            file_path=str(save_path.resolve()),
            file_size=total_size,
            source="upload",
            status="uploaded",
        )
    except Exception as e:
        print(f"[MySQL] 视频记录写入失败: {e}")

    file_size_mb = total_size / (1024 * 1024)
    renamed = save_path.name != safe_name

    return {
        "status": "ok",
        "filename": save_path.name,
        "path": str(save_path.resolve()),
        "size_mb": round(file_size_mb, 2),
        "container": container_name,
        # 同名时自动改名了要说清楚，否则前端以为"东西没传上去"
        "renamed": renamed,
        "original_filename": safe_name if renamed else None,
        "message": ("文件已上传"
                    + (f"（同名文件已存在，另存为 {save_path.name}）" if renamed else "")
                    + "，可通过 WebSocket 发送播放指令"),
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
async def update_zone(zone: dict,
                      _zone_admin: dict = Depends(require_perm("system:manage"))):
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
async def delete_zone(zone_id: str,
                      _zone_admin: dict = Depends(require_perm("system:manage"))):
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


@app.get("/api/cameras/hardware", include_in_schema=False)
async def scan_cameras():
    """扫描服务器上可用的摄像头设备（与 /api/cameras 注册表列表区分，避免路由遮蔽）"""
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
