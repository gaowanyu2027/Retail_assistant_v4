"""只读角色（viewer）与"写路由必须挂权限"的回归守卫（2026-09-22）。

## 背景：为什么要有这个角色

此前只有 `root` / `platform` 两个角色，而 **`platform` 带 `data:write`**（导入销量、生成演示数据、
启停采集…）。把这种账号发给外部使用者/演示对象，等于让他能**改掉你准备好的演示数据**。
`viewer` 只拿 `data:read`：能问答、看看板/热度/告警/表情，但**所有写接口一律 403**。

## 为什么还要守卫"写路由有没有挂权限"

台账 B2 的教训是"权限矩阵声明了、实际没校验"。加只读角色时我做了一次逐个复核：
`api/routes/*.py` 里 **39 条写路由**，其中 22 条挂了 `require_perm`（`data:write` 7 / `system:manage` 14 /
`user:manage` 4 —— 有重叠计数），另外 17 条属于"登录即可"的**有意白名单**
（登录/登出、改自己口令、自己的会话 CRUD、问答、地图查询、语音指令）。
本文件把这份结论**固化成断言**：以后新增写接口若忘了挂权限，CI 会直接红。
"""
import io
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ROUTES_DIR = PROJECT_ROOT / "api" / "routes"

_DEC = re.compile(r"^@router\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]")
_PERM = re.compile(r"require_perm\(\s*['\"]([^'\"]+)['\"]")

# 「登录即可」的有意白名单：这些写路由**不该**要求额外权限，逐条给出理由。
# 键 = (文件名, 路径)；漏挂权限的新写路由若不在此表，测试即失败。
INTENTIONAL_LOGIN_ONLY = {
    ("auth.py", "/auth/login"): "登录本身当然不能要求登录/权限",
    ("auth.py", "/auth/logout"): "登出只吊销**自己**的会话",
    ("auth.py", "/auth/ws-ticket"): "签发**自己**的 WS 一次性票据",
    ("auth.py", "/auth/password"): "改**自己**的口令（要旧口令）",
    ("auth.py", "/auth/sessions/{session_id}"): "吊销**自己**的登录态（服务端按归属校验）",
    ("chat.py", "/sessions"): "新建**自己**的会话（owner = 当前账号）",
    ("chat.py", "/sessions/{session_id}/save"): "保存**自己**的会话（owner 校验）",
    ("chat.py", "/sessions/{session_id}/rename"): "重命名**自己**的会话（owner 校验）",
    ("chat.py", "/sessions/{session_id}"): "删除**自己**的会话（owner 校验）",
    ("query.py", "/queries"): "问答入口：viewer 本来就该能问（只写自己的会话记录）",
    ("query.py", "/queries/stream"): "问答入口（SSE 流式）",
    ("query.py", "/query"): "问答入口（兼容别名）",
    ("query.py", "/query/stream"): "问答入口（兼容别名，流式）",
    ("maps.py", "/geocode"): "只读查询（百度地图地理编码）",
    ("maps.py", "/distance"): "只读查询（距离计算）",
    ("voice.py", "/commands"): "语音指令：内部只做只读/问答（见 _voice_command_impl）",
    ("voice.py", "/command"): "语音指令（兼容别名）",
}

# 这些文件里的写路由**必须**是某个权限（业务数据 / 设备控制 / 账号管理），一条都不能漏
FAMILY_REQUIRED_PERM = {
    "analytics.py": "data:write",
    "cameras.py": "system:manage",
    "multi_stream.py": "system:manage",
    "emotion_camera.py": "system:manage",
}


def _scan_write_routes():
    """扫描 api/routes/*.py，返回 [(文件, 行号, 方法, 路径, 权限集合)]（只含非 GET）。"""
    out = []
    for f in sorted(ROUTES_DIR.glob("*.py")):
        lines = io.open(f, encoding="utf-8").read().splitlines()
        for i, raw in enumerate(lines):
            m = _DEC.match(raw.strip())
            if not m or m.group(1).upper() == "GET":
                continue
            perms = set()
            for s in lines[i + 1: i + 40]:
                if s.strip().startswith("@router."):
                    break
                perms |= set(_PERM.findall(s))
                # 进入函数体后停止（依赖都写在签名里）
                if re.match(r"^\s{4,}(return|await |try:|with |if |[A-Za-z_]+\s*=)", s):
                    break
            out.append((f.name, i + 1, m.group(1).upper(), m.group(2), perms))
    return out


def test_viewer_role_matrix():
    """viewer 只能读：没有任何写/管理权限。"""
    from api.security import ROLES, ROLE_LABELS, ROLE_VIEWER, perms_of, has_perm

    assert ROLE_VIEWER in ROLES, "viewer 未登记进 ROLES"
    assert ROLE_VIEWER in ROLE_LABELS, "viewer 缺少界面展示用的中文标签"
    assert set(perms_of(ROLE_VIEWER)) == {"data:read"}, f"viewer 权限不止读：{perms_of(ROLE_VIEWER)}"
    for perm in ("data:write", "user:manage", "system:manage"):
        assert not has_perm(ROLE_VIEWER, perm), f"viewer 不该拥有 {perm}"


def test_root_and_platform_unchanged():
    """新增角色不能顺手改掉原有角色的权限（回归）。"""
    from api.security import perms_of

    assert set(perms_of("root")) == {"data:read", "data:write", "user:manage", "system:manage"}
    assert set(perms_of("platform")) == {"data:read", "data:write"}


def test_every_write_route_is_either_protected_or_whitelisted():
    """每条写路由：要么挂了 require_perm，要么在**有意白名单**里（附理由）。"""
    offenders = []
    for fname, line, method, path, perms in _scan_write_routes():
        if perms:
            continue
        if (fname, path) in INTENTIONAL_LOGIN_ONLY:
            continue
        offenders.append(f"{fname}:{line} {method} {path}")
    assert not offenders, (
        "以下写路由既没挂 require_perm，也不在白名单里 —— 要么补权限，要么加进白名单并写明理由：\n  "
        + "\n  ".join(offenders)
    )


def test_sensitive_route_families_all_protected():
    """业务数据写入 / 设备控制类路由**必须**逐条挂权限（不允许漏）。"""
    problems = []
    for fname, line, method, path, perms in _scan_write_routes():
        need = FAMILY_REQUIRED_PERM.get(fname)
        if need and need not in perms:
            problems.append(f"{fname}:{line} {method} {path} → 缺少 {need}（实际 {perms or '无'}）")
    assert not problems, "\n  ".join(problems)


def test_whitelist_has_no_stale_entries():
    """白名单不能留"已经挂了权限"或"路由已删除"的死条目（否则守卫会慢慢失真）。"""
    live = {(f, p) for f, _, _, p, perms in _scan_write_routes() if not perms}
    stale = sorted(set(INTENTIONAL_LOGIN_ONLY) - live)
    assert not stale, f"白名单里的这些条目已不再匹配任何'无权限写路由'，应删除：{stale}"
