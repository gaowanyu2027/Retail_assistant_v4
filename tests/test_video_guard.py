"""视频源安全守卫测试（台账 B1 / B6 的绕过清单）。

这些都是**纯函数**，不需要起服务、不需要网络（只有主机名校验会走 DNS，
其中 `localhost` 用例依赖系统把 localhost 解析到环回 —— 这是标准行为）。
"""
import contextlib
import ipaddress
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import video_sources as vs  # noqa: E402

# 仓库自带的极小视频夹具（约 4 KB，可解码）。
# ⚠ 不要改回 `mmpose/demo/resources/demo.mp4` 或 `data/sources/*.mp4`：
#   前者在 .gitignore 的 `mmpose/` 里、后者整个 `data/` 被忽略 —— **全新 clone / CI
#   上都不存在**，用例会"假失败"（看起来像守卫坏了，其实是测试依赖了本地才有的文件）。
FIXTURE = PROJECT_ROOT / "tests" / "assets" / "tiny.mp4"


def _rel(p: Path) -> str:
    """转成仓库相对路径，统一正斜杠（跨平台一致）。"""
    return p.relative_to(PROJECT_ROOT).as_posix()


@contextlib.contextmanager
def _allowlisted_media(name: str = "_guard_fixture.mp4"):
    """在**允许目录**（data/sources）里临时放一个真视频，用完删掉。

    `data/` 被 .gitignore 忽略，所以这类用例必须运行时自己造文件。
    """
    d = PROJECT_ROOT / "data" / "sources"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(FIXTURE.read_bytes())
    try:
        yield f
    finally:
        try:
            f.unlink()
        except OSError:
            pass


def _expect_reject(fn, code: str | None = None):
    try:
        out = fn()
    except vs.SourceError as e:
        if code:
            assert e.code == code, f"错误码应为 {code}，实际 {e.code}（{e}）"
        return e
    raise AssertionError(f"应当被拒绝，但通过了：{out!r}")


def _expect_pass(fn):
    try:
        return fn()
    except vs.SourceError as e:
        raise AssertionError(f"应当放行，却被拒绝 [{e.code}] {e}")


# ---------------- URL / 主机 绕过清单 ----------------

def test_reject_loopback_and_metadata():
    """环回、链路本地（含云元数据）、组播、保留、未指定 —— 永久封禁。"""
    for url in [
        "rtsp://127.0.0.1:554/x",
        "rtsp://localhost:554/x",              # 域名解析到环回（DNS 重绑定式）
        "rtsp://[::1]:554/x",
        "rtsp://169.254.169.254/latest/meta-data/",   # 云元数据
        "rtsp://224.0.0.1/x",                  # 组播
        "rtsp://0.0.0.0/x",                    # 未指定
    ]:
        _expect_reject(lambda u=url: vs.guard_url(u), "host_blocked")


def test_reject_ipv4_mapped_ipv6():
    """IPv6 映射写法必须先还原成 IPv4 再判断，否则能绕过环回封禁。"""
    _expect_reject(lambda: vs.guard_url("rtsp://[::ffff:127.0.0.1]:554/x"), "host_blocked")


def test_reject_alternate_ip_notations():
    """十进制 / 短写 IP：本平台 getaddrinfo 直接解析失败 → 拒绝（不能放行）。"""
    _expect_reject(lambda: vs.guard_url("rtsp://2130706433:554/x"))
    _expect_reject(lambda: vs.guard_url("rtsp://127.1:554/x"))


def test_reject_public_by_default():
    _expect_reject(lambda: vs.guard_url("rtsp://8.8.8.8:554/x"), "host_blocked")


def test_reject_bad_scheme_and_unc_and_junk():
    _expect_reject(lambda: vs.guard_url("file:///etc/passwd"), "unsupported_scheme")
    _expect_reject(lambda: vs.guard_url("http://192.168.1.10/x"), "unsupported_scheme")
    _expect_reject(lambda: vs.guard_url("rtsp://x\\\\y"), "unc_not_allowed")
    _expect_reject(lambda: vs.guard_url("rtsp://192.168.1.10/x\nHost: evil"), "bad_request")
    _expect_reject(lambda: vs.guard_url("rtsp://192.168.1.10/" + "a" * 600), "bad_request")
    _expect_reject(lambda: vs.guard_url(""), "bad_request")


def test_allow_private_lan_camera():
    """门店摄像头在内网 —— 私网默认必须放行，否则把正常用法打死。"""
    assert _expect_pass(lambda: vs.guard_url("rtsp://192.168.10.20:554/stream")) \
        == "rtsp://192.168.10.20:554/stream"


def test_mask_credentials():
    """凭据不能明文进日志/回执。"""
    masked = vs.mask_credentials("rtsp://admin:secret@192.168.10.20:554/s")
    assert "secret" not in masked and "admin" not in masked, masked
    assert masked.startswith("rtsp://***:***@"), masked
    assert vs.mask_credentials("/app/data/x.mp4") == "/app/data/x.mp4"


# ---------------- 路径 ----------------

def test_client_path_must_be_in_allowlist():
    """B6：客户端给的路径必须在白名单目录内（越界/`..` 折回都要拒）。"""
    _expect_reject(lambda: vs.guard_path("/app/mmpose/demo/resources/demo.mp4", client_supplied=True),
                   "file_not_allowed")
    _expect_reject(lambda: vs.guard_path("/app/data/../mmpose/demo/resources/demo.mp4",
                                         client_supplied=True), "file_not_allowed")
    _expect_reject(lambda: vs.guard_path("\\\\attacker\\share\\x.mp4", client_supplied=True),
                   "unc_not_allowed")
    _expect_reject(lambda: vs.guard_path("data/sources/nope.mp4", client_supplied=True),
                   "file_not_found")


def test_configured_path_is_not_allowlist_limited():
    """服务端配置的路径（root 可信）不受白名单约束，但必须存在且非 UNC。"""
    assert FIXTURE.is_file(), f"缺少测试夹具 {FIXTURE}（应为仓库内的极小 mp4）"
    rel = _rel(FIXTURE)
    full = _expect_pass(lambda: vs.guard_path(rel, client_supplied=False))
    assert full.exists() and full == FIXTURE.resolve(), full
    # 同一条路径换成"客户端提交"就必须被白名单拦下 —— B1/B6 两级策略的差异就在这一行
    _expect_reject(lambda: vs.guard_path(rel, client_supplied=True), "file_not_allowed")
    _expect_reject(lambda: vs.guard_path("\\\\a\\b.mp4", client_supplied=False), "unc_not_allowed")


def test_guard_source_device_passthrough():
    for s in ("webcam", "0", "2"):
        assert vs.guard_source(s) == s


# ---------------- 统一入口 normalize ----------------

def test_normalize_rtsp_is_guarded_not_blocked():
    """客户端直传 RTSP：**校验后放行**（不是"一律拒绝"）。

    早期版本这里是一律拒绝（那时还没有守卫）；现在守卫能拦住 SSRF 目标，
    所以合法私网地址放行、危险目标照旧拒绝。这条测试把两半**都**钉住。
    """
    # 合法私网 → 放行
    src = _expect_pass(lambda: vs.normalize({"kind": "rtsp", "url": "rtsp://192.168.10.20:554/s"}))
    assert src["legacy_action"] == "start_file"
    assert src["params"]["file_path"] == "rtsp://192.168.10.20:554/s"

    # 危险目标 → 仍必须拒
    _expect_reject(lambda: vs.normalize({"kind": "rtsp", "url": "rtsp://169.254.169.254/x"}),
                   "host_blocked")
    _expect_reject(lambda: vs.normalize({"kind": "rtsp", "url": "rtsp://127.0.0.1/x"}),
                   "host_blocked")
    _expect_reject(lambda: vs.normalize({"kind": "rtsp", "url": "rtsp://8.8.8.8/x"}),
                   "host_blocked")

    # 缺 url / 未知 kind / 缺 source
    _expect_reject(lambda: vs.normalize({"kind": "rtsp"}), "bad_request")
    _expect_reject(lambda: vs.normalize({"kind": "nope"}), "unsupported_kind")
    _expect_reject(lambda: vs.normalize(None), "bad_request")


def test_client_url_kill_switch():
    """`VIDEO_SOURCE_ALLOW_CLIENT_URL=0` 时应拒绝客户端直传的流地址（可回滚的开关）。"""
    old = vs.VIDEO_SOURCE_ALLOW_CLIENT_URL
    try:
        vs.VIDEO_SOURCE_ALLOW_CLIENT_URL = False
        _expect_reject(lambda: vs.normalize({"kind": "rtsp", "url": "rtsp://192.168.10.20:554/s"}),
                       "client_url_disabled")
    finally:
        vs.VIDEO_SOURCE_ALLOW_CLIENT_URL = old


def test_normalize_file_kind_uses_guard():
    with _allowlisted_media() as f:          # 允许目录内的真文件（运行时造，不入库）
        src = _expect_pass(lambda: vs.normalize({"kind": "file", "path": _rel(f)}))
        assert src["legacy_action"] == "start_file"
    _expect_reject(lambda: vs.normalize({"kind": "file", "path": "../secret.mp4"}),
                   "file_not_allowed")


def test_normalize_upload_only_filename():
    """upload 只接受文件名；路径穿越会被 basename 化后判为不存在（不会放行）。"""
    _expect_reject(lambda: vs.normalize({"kind": "upload", "id": "../../etc/passwd.mp4"}))
    _expect_reject(lambda: vs.normalize({"kind": "upload", "id": ""}), "bad_request")


def test_echo_for_legacy():
    assert vs.echo_for_legacy("start_file", {"file_path": "/x.mp4"})["kind"] == "file"
    assert vs.echo_for_legacy("start_client_camera", {})["kind"] == "client"
    assert vs.echo_for_legacy("ping", {}) == {}
