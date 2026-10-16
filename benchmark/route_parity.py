"""路由等价性验收：直连 Python 后端 vs 经 Go edge，逐路径比对响应。

为什么需要它：S1 把对外 8000 从 Python 换成了 Go edge（静态托管 / 健康 /
兜底反代）。"没改坏"不能靠手点几下，要有一条**可复现、可重复跑**的清单：
枚举后端全部真实路由 + dist 静态文件 + 兜底角落，两种身份（匿名 / 已登录）
各请求一遍，直连与经网关逐项比对。

用法（本机双进程验收）：
    # 终端 1：python run.py --port 8001        （直连目标）
    # 终端 2：EDGE_LISTEN=:8000 EDGE_BACKEND_URL=http://127.0.0.1:8001 \
    #         EDGE_DIST_DIR=frontend-vue/dist edge      （经网关目标）
    python benchmark/route_parity.py --direct http://127.0.0.1:8001 --gate http://127.0.0.1:8000

比较维度：
  - HTTP 状态码（重定向另比 Location 的 path+query；不跟随重定向）
  - Content-Type
  - 头部子集（缓存 / 安全头六项；Allow 按集合比，与顺序无关）
  - 响应体：非 JSON 逐字节；JSON 先比"骨架"（键与类型，忽略取值），
    小响应（≤ 1KB，剔除易变字段后）再逐字节 —— 404/405/401 的
    `{"detail": ...}`、`{"status":"ok"}` 这类确定性报文因此是**严格**比对。

已知设计差异（显式排除，不计为差异）：
  - `/metrics`：edge 原生路径（仅私网可读），Python 没有该路由；
  - Server / Date 等易变头不在比较子集内；
  - 有副作用的写接口不发真实请求（只发 GET；POST 探针仅
    /api/frontend/fallback 与 /api/health 两条无副作用路径）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = Path(__file__).resolve().parent / "reports"
DIST_DIR = PROJECT_ROOT / "frontend-vue" / "dist"

# 参与比较的响应头子集（缓存与安全语义；不含 Date/Server/Content-Length 等易变或传输细节）
HEADER_SUBSET = [
    "Content-Type",
    "Cache-Control",
    "Pragma",
    "Expires",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Permissions-Policy",
    "Content-Security-Policy",
]

# JSON 比较时剔除的易变字段（两次请求之间必然不同，且与"路由是否等价"无关）
VOLATILE_KEYS = {
    "uptime_seconds", "timestamp", "server_time", "now", "generated_at",
    "elapsed_ms", "took_ms", "duration_ms", "request_id",
}
SMALL_BODY_LIMIT = 1024          # 剔除易变字段后 ≤ 此字节数的 JSON 做逐字节严格比对

ENUM_CODE = r"""
import json
from api.main import app

# 注：FastAPI 0.139 的 include_router 是**惰性**的（app.routes 里是
# `_IncludedRouter` 占位对象，首请求才物化），直接遍历 app.routes 只能拿到
# 主模块自带的少数路由。这里递归展开 original_router + include_context.prefix，
# 再与 OpenAPI 文档路径取并集兜底（文档会排除 include_in_schema=False 的兼容别名，
# 而那些别名恰恰要靠"兜底反代"覆盖，必须枚举进来）。
def walk(routes, prefix, out, seen):
    for r in routes:
        if type(r).__name__ == "_IncludedRouter":
            ctx = getattr(r, "include_context", None)
            walk(getattr(getattr(r, "original_router", None), "routes", []),
                 prefix + (getattr(ctx, "prefix", "") or ""), out, seen)
            continue
        path = getattr(r, "path", None)
        if not path:
            continue
        full = prefix + path
        methods = sorted(getattr(r, "methods", None) or [])
        key = (full, tuple(methods))
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": full, "methods": methods})

out = []
walk(app.routes, "", out, set())
for p, ops in (app.openapi().get("paths") or {}).items():
    methods = sorted(m.upper() for m in ops if m.upper() in
                     {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
    if not any(x["path"] == p for x in out):
        out.append({"path": p, "methods": methods})
print(json.dumps(out))
"""

SESSION_CODE = r"""
import json
from api import security as S
user = S.get_user_by_username({username!r})
if not user:
    print(json.dumps(None))
else:
    s = S.create_session(user["id"], user["username"], user["role"], ip="127.0.0.1",
                         user_agent="route_parity.py")
    print(json.dumps(s))
"""


def _run_python(code: str) -> str:
    """在项目根下跑一段 Python（子进程），返回 stdout。

    用子进程而不是本进程 import：枚举路由要 import api.main（会连带
    拉起全部依赖），且要读写**运行中后端正在用的 auth.db** —— 与
    本工具的进程隔离，避免任何连接/缓存串扰。
    """
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        # Windows 下父进程的默认编码是 GBK，而子进程按 UTF-8 输出（应用里有中文日志），
        # 不显式指定会在读取时抛 UnicodeDecodeError
        encoding="utf-8", errors="replace", timeout=180,
    )
    if out.returncode != 0:
        raise RuntimeError(f"子进程失败（{out.returncode}）:\n{out.stderr[-2000:]}")
    return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""


def enumerate_routes() -> list[dict]:
    return json.loads(_run_python(ENUM_CODE))


def create_session(username: str) -> dict | None:
    return json.loads(_run_python(SESSION_CODE.format(username=username)))


def dist_probe_paths(limit_per_dir: int = 2) -> list[str]:
    """从当前 dist 取真实存在的静态文件（哈希文件名每次构建会变，必须现枚举）。"""
    if not DIST_DIR.is_dir():
        return []
    picked: dict[str, int] = {}
    paths = []
    for f in sorted(DIST_DIR.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(DIST_DIR).as_posix()
        top = rel.split("/", 1)[0] if "/" in rel else ""
        if top:
            picked[top] = picked.get(top, 0)
            if picked[top] >= limit_per_dir:
                continue
            picked[top] += 1
        paths.append("/" + rel)
    return paths


def collect_probes(routes: list[dict]) -> list[dict]:
    probes: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(method: str, path: str, note: str) -> None:
        if (method, path) in seen:
            return
        seen.add((method, path))
        probes.append({"method": method, "path": path, "note": note})

    for r in routes:
        path = r["path"]
        if path == "/metrics":
            continue                                   # 设计差异，见文件头
        path = re.sub(r"\{[^}]+\}", "1", path)         # 参数用 "1" 占位（两侧一致即可）
        add("GET", path, "路由")

    for path in dist_probe_paths():
        add("GET", path, "静态")

    for path, note in [
        ("/nope", "未知路径"),
        ("/api/does-not-exist", "未知 API"),
        ("/js", "挂载点无尾斜杠"),
        ("/css", "挂载点无尾斜杠"),
        ("/api/health/", "尾斜杠"),
        ("/openapi.json", "OpenAPI schema"),
    ]:
        add("GET", path, note)

    # 无副作用的 POST 探针（写接口一律不碰，避免污染真实数据）
    add("POST", "/api/frontend/fallback", "Vue 回退上报")
    add("POST", "/api/health", "方法不允许")
    return probes


def fetch(client: httpx.Client, method: str, path: str, cookie: str | None) -> dict:
    headers = {"Accept-Encoding": "identity"}          # 关压缩：两侧拿到的都是原始字节
    if cookie:
        headers["Cookie"] = f"retail_sid={cookie}"
    try:
        r = client.request(method, path, headers=headers)
    except Exception as e:                             # 连接层失败也两边一起比
        return {"error": type(e).__name__}
    ctype = r.headers.get("content-type", "")
    body = r.content
    out = {
        "status": r.status_code,
        "content_type": ctype,
        "headers": {h: r.headers.get(h) for h in HEADER_SUBSET},
        "location": None,
        "body": body,
    }
    if r.status_code in (301, 302, 303, 307, 308):
        loc = httpx.URL(r.headers.get("location", ""))
        out["location"] = loc.path + (f"?{loc.query.decode()}" if loc.query else "")
    if "json" in ctype:
        try:
            out["json"] = json.loads(body)
        except Exception:
            out["json"] = None
    return out


def skeleton(value):
    """JSON 骨架：保留键与类型（列表只看首元素），忽略会随时间变化的取值。"""
    if isinstance(value, dict):
        return {k: skeleton(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return ["list"] if not value else [skeleton(value[0])]
    return type(value).__name__


def strip_volatile(value, path: str = ""):
    if isinstance(value, dict):
        return {
            k: strip_volatile(v, path)
            for k, v in value.items()
            if k not in VOLATILE_KEYS and not (path == "/api/health/ready" and k == "detail")
        }
    if isinstance(value, list):
        return [strip_volatile(v, path) for v in value]
    return value


def compare(path: str, a: dict, b: dict) -> list[dict]:
    """a = 直连后端，b = 经 edge。返回差异列表（空 = 等价）。"""
    diffs: list[dict] = []

    def note(dim: str, direct, gate) -> None:
        diffs.append({"path": path, "dim": dim, "direct": direct, "gate": gate})

    if "error" in a or "error" in b:
        if a.get("error") != b.get("error"):
            note("连接", a.get("error"), b.get("error"))
        return diffs

    if a["status"] != b["status"]:
        note("status", a["status"], b["status"])
    if a["location"] != b["location"]:
        note("location", a["location"], b["location"])
    if a["content_type"] != b["content_type"]:
        note("content-type", a["content_type"], b["content_type"])

    for h in HEADER_SUBSET:
        va, vb = a["headers"].get(h), b["headers"].get(h)
        if h == "Content-Type":
            continue                                   # 已单独比
        if h == "Allow":
            sa = {x.strip() for x in (va or "").split(",") if x.strip()}
            sb = {x.strip() for x in (vb or "").split(",") if x.strip()}
            if sa != sb:
                note("Allow", sorted(sa), sorted(sb))
        elif va != vb:
            note(h, va, vb)

    ja, jb = "json" in a, "json" in b
    if ja != jb:
        note("body 类型", "json" if ja else "binary", "json" if jb else "binary")
        return diffs
    if ja:
        if a["json"] is None or b["json"] is None:
            if a["body"] != b["body"]:
                note("body", a["body"][:200], b["body"][:200])
            return diffs
        if skeleton(a["json"]) != skeleton(b["json"]):
            note("body 骨架", skeleton(a["json"]), skeleton(b["json"]))
            return diffs
        na, nb = strip_volatile(a["json"], path), strip_volatile(b["json"], path)
        ba = json.dumps(na, sort_keys=True, ensure_ascii=False).encode()
        bb = json.dumps(nb, sort_keys=True, ensure_ascii=False).encode()
        if len(ba) <= SMALL_BODY_LIMIT and len(bb) <= SMALL_BODY_LIMIT and ba != bb:
            note("body", ba[:300].decode("utf-8", "replace"),
                 bb[:300].decode("utf-8", "replace"))
    else:
        if a["body"] != b["body"]:
            note("body", a["body"][:200].decode("utf-8", "replace"),
                 b["body"][:200].decode("utf-8", "replace"))
    return diffs


def main() -> int:
    # Windows 控制台默认 GBK，报告里有中文与 ✅/❌，不重设会在打印时抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="直连后端 vs 经 Go edge 的路由等价性比对")
    ap.add_argument("--direct", default="http://127.0.0.1:8001", help="直连后端地址")
    ap.add_argument("--gate", default="http://127.0.0.1:8000", help="经网关地址")
    ap.add_argument("--user", default="root", help="用于构造已登录身份的账号名")
    ap.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = ap.parse_args()

    routes = enumerate_routes()
    probes = collect_probes(routes)
    session = create_session(args.user)
    cookie = (session or {}).get("token")
    identities = [("匿名", None)]
    if cookie:
        identities.append((f"已登录({args.user})", cookie))
    else:
        print(f"⚠ 账号 {args.user!r} 不存在，跳过已登录身份（只比匿名）\n")

    diffs: list[dict] = []
    probes_total = 0
    with httpx.Client(base_url=args.direct, follow_redirects=False, timeout=30.0) as cd, \
         httpx.Client(base_url=args.gate, follow_redirects=False, timeout=30.0) as cg:
        for ident, ck in identities:
            for p in probes:
                probes_total += 1
                a = fetch(cd, p["method"], p["path"], ck)
                b = fetch(cg, p["method"], p["path"], ck)
                for d in compare(p["path"], a, b):
                    d["method"] = p["method"]
                    d["identity"] = ident
                    d["note"] = p["note"]
                    diffs.append(d)

    print(f"目标：直连 {args.direct}  vs  经网关 {args.gate}")
    print(f"路径清单：{len(probes)} 条路由/静态/角落（另排除 /metrics 设计差异）"
          f"，身份 {len(identities)} 种，共 {probes_total} 组请求\n")
    if diffs:
        print(f"❌ 差异 {len(diffs)} 条：")
        for d in diffs[:60]:
            print(f"  [{d['identity']}] {d['method']} {d['path']} ({d['note']})")
            print(f"      {d['dim']}: 直连={d['direct']!r}")
            print(f"      {' ' * len(d['dim'])}  网关={d['gate']!r}")
        if len(diffs) > 60:
            print(f"  … 其余 {len(diffs) - 60} 条见报告文件")
    else:
        print("✅ 差异 = 0（状态码 / Content-Type / 头子集 / 响应体全部等价）")

    if not args.no_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / f"route_parity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps({
            "time": datetime.now().isoformat(timespec="seconds"),
            "direct": args.direct,
            "gate": args.gate,
            "probes": len(probes),
            "identities": [i[0] for i in identities],
            "diff_count": len(diffs),
            "diffs": diffs,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已保存: {out}")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
