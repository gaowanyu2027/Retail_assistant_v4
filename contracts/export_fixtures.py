"""重新导出 contracts/*.json 夹具（从当前实现**实测**导出，不手抄）。

何时需要跑：
    - 有意变更了权限矩阵 / 守卫行为 / WS 协议：先改实现与测试，再跑本脚本更新夹具，
      提交里的夹具 diff 就是这个变更的显式化；
    - 平时**不要**跑：tests/test_contract_fixtures.py 与 tests/test_ws_protocol_fixtures.py
      会比对夹具与实现，夹具漂移即测试失败。

用法（在仓库根，任意 Python 3.10+ 环境）：
    python contracts/export_fixtures.py
"""
import json
import sys
import warnings
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONTRACTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import WS_METADATA_INTERVAL  # noqa: E402
import video_sources as vs  # noqa: E402
import ws_protocol as wsp  # noqa: E402

# 这些 URL 的**错误码**随平台解析行为不同（Windows getaddrinfo 直接失败=dns_failed；
# Linux 兼容十进制/短写 IP 会解析成功进而被 host_blocked）——共同契约是"必须拒绝"。
_PLATFORM_VARIANT_URLS = {
    "rtsp://2130706433:554/x",
    "rtsp://127.1:554/x",
}


def _dump(name: str, obj: dict) -> None:
    path = CONTRACTS_DIR / name
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    print(f"[export] {path.relative_to(PROJECT_ROOT)}")


# ---------------- guard_cases.json ----------------

def _url_case(url: str, note: str | None = None) -> dict:
    item = {"url": url}
    try:
        item["expect"] = {"ok": vs.guard_url(url)}
    except vs.SourceError as e:
        if url in _PLATFORM_VARIANT_URLS:
            item["expect"] = {"error_in": ["dns_failed", "host_blocked"]}
        else:
            item["expect"] = {"error": e.code}
    if note:
        item["note"] = note
    return item


def export_guard_cases() -> dict:
    url_cases = [
        _url_case("rtsp://127.0.0.1:554/x"),
        _url_case("rtsp://localhost:554/x", "依赖系统把 localhost 解析到环回（标准行为）"),
        _url_case("rtsp://[::1]:554/x"),
        _url_case("rtsp://169.254.169.254/latest/meta-data/", "云元数据（链路本地）"),
        _url_case("rtsp://224.0.0.1/x", "组播"),
        _url_case("rtsp://0.0.0.0/x", "未指定地址"),
        _url_case("rtsp://[::ffff:127.0.0.1]:554/x", "IPv4 映射 IPv6：必须先还原成 IPv4 再判断"),
        _url_case("rtsp://2130706433:554/x", "十进制 IP"),
        _url_case("rtsp://127.1:554/x", "短写 IP"),
        _url_case("rtsp://8.8.8.8:554/x", "公网地址默认拒绝（门店摄像头在内网）"),
        _url_case("rtsp://192.168.10.20:554/stream", "私网相机：正常用法必须放行"),
        _url_case("file:///etc/passwd"),
        _url_case("http://192.168.1.10/x"),
        _url_case("rtsp://x\\y", "UNC 形态"),
        _url_case("rtsp://192.168.1.10/x\nHost: evil", "换行注入"),
        _url_case("rtsp://192.168.1.10/" + "a" * 600, "超长 URL"),
        _url_case(""),
    ]

    path_cases = []
    for path, client_supplied in [
        ("/app/mmpose/demo/resources/demo.mp4", True),
        ("/app/data/../mmpose/demo/resources/demo.mp4", True),
        ("\\\\attacker\\share\\x.mp4", True),
        ("data/sources/nope.mp4", True),
        ("\\\\a\\b.mp4", False),
        ("../secret.mp4", True),
    ]:
        item = {"path": path, "client_supplied": client_supplied}
        try:
            item["expect"] = {"ok": str(vs.guard_path(path, client_supplied=client_supplied))}
        except vs.SourceError as e:
            item["expect"] = {"error": e.code}
        path_cases.append(item)

    source_cases = [{"in": s, "expect": {"ok": vs.guard_source(s)}} for s in ("webcam", "0", "2")]

    normalize_cases = []
    for src in [
        {"kind": "rtsp", "url": "rtsp://192.168.10.20:554/s"},
        {"kind": "rtsp", "url": "rtsp://169.254.169.254/x"},
        {"kind": "rtsp", "url": "rtsp://127.0.0.1/x"},
        {"kind": "rtsp", "url": "rtsp://8.8.8.8/x"},
        {"kind": "rtsp"},
        {"kind": "nope"},
        None,
    ]:
        item = {"in": src}
        try:
            item["expect"] = {"ok": vs.normalize(json.loads(json.dumps(src)))}
        except vs.SourceError as e:
            item["expect"] = {"error": e.code}
        normalize_cases.append(item)

    mask_cases = []
    for text in ("rtsp://admin:secret@192.168.10.20:554/s", "/app/data/x.mp4"):
        mask_cases.append({"in": text, "expect": {"ok": vs.mask_credentials(text)}})

    echo_cases = []
    for action, params in (("start_file", {"file_path": "/x.mp4"}),
                           ("start_client_camera", {}),
                           ("ping", {})):
        echo_cases.append({"action": action, "params": params,
                           "expect": {"ok": vs.echo_for_legacy(action, params)}})

    return {
        "schema": 1,
        "generated_by": "contracts/export_fixtures.py",
        "source": "video_sources.py",
        "note": ("守卫的唯一来源是 video_sources.py（安全关键代码不复制）。这些案例是跨语言 "
                 "golden：任何调用方（Python 现有实现 / 将来的 Go 边缘）对同一输入必须给出"
                 "相同的放行或拒绝结论；拒绝时 error 即 SourceError.code。"),
        "guard_url": url_cases,
        "guard_path": path_cases,
        "guard_source": source_cases,
        "normalize": normalize_cases,
        "mask_credentials": mask_cases,
        "echo_for_legacy": echo_cases,
    }


# ---------------- permissions.json ----------------

def export_permissions() -> dict:
    import api.security as sec

    return {
        "schema": 1,
        "generated_by": "contracts/export_fixtures.py",
        "source": "api/security.py（ROLES / ROLE_LABELS / PERMISSIONS；perms_of() 是唯一读取入口）",
        "note": ("角色三档与权限矩阵是安全契约：服务端 require_perm(...) 强制，前端置灰只是体验。"
                 "任何改动必须同步更新本夹具，让 diff 显式可见。"),
        "roles": list(sec.ROLES),
        "role_labels": {r: sec.ROLE_LABELS[r] for r in sec.ROLES},
        "perms_of": {r: sec.perms_of(r) for r in sec.ROLES},
    }


# ---------------- ws_messages.json ----------------

def export_upstream_envelopes() -> dict:
    """上行帧流（Python→Go）的信封样本：**实测**调用 `_envelope` 导出，不手抄。

    Go 侧的解析器与它成对；这一节就是"两侧字节相同"的判据。
    """
    from api.routes import vision_internal as vi

    jpeg = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
    frame_meta = wsp.dumps_message({
        "gen": 1, "frame_id": 617,
        # 帧元数据 WS 文本：Go 只做转发，**不解包也不重组**
        "msg": wsp.dumps_message({"type": "frame", "frame_id": 617, "mode": "retail"}),
    })
    control_meta_stop = wsp.dumps_message({
        "gen": 2, "clear": True, "except": "v3",
        "framed_tail": wsp.dumps_message({"type": "status", "status": "stopped",
                                          "message": "视频流已停止"}),
        "texts": [wsp.dumps_message({"type": "status", "status": "stopped",
                                     "message": "视频已停止，WebSocket 保持连接",
                                     "segment_analysis": None}),
                  wsp.dumps_message(wsp.source_status_message(
                      "stopped", {"kind": "file", "path": "/x.mp4"},
                      message="视频已停止"))],
    })
    control_meta_error = wsp.dumps_message({
        "gen": 2, "clear": True, "except": None, "framed_tail": None,
        "texts": [wsp.dumps_message(wsp.source_status_message(
            "error", {}, code="open_timeout", message="打开超时：……"))],
    })
    # kind=4：入库事件（S4）。字段契约见 contracts/ingest_events.json；
    # 这里只验证信封形状与"gen/seq 在 meta 里"这一位置，不重复整份样例。
    ingest_meta = wsp.dumps_message({
        "gen": 2, "seq": 9,
        "alerts": [{"alert_type": "anomaly", "zone_id": "shelf_1",
                    "person_id": 7, "level": "watch", "score": 5,
                    "reason": "在货架区长时间徘徊", "frame_id": 123,
                    "created_at": "2026-10-17 16:05:01"}],
    })

    cases = [
        ("frame", vi._KIND_FRAME, 617, frame_meta.encode("utf-8"), jpeg,
         "帧：meta 是 JSON 文本（含 gen/frame_id/msg），jpeg 是原始帧字节"),
        ("control_stop", vi._KIND_CONTROL, 0, control_meta_stop.encode("utf-8"), b"",
         "控制：停止（含 texts / framed_tail / clear / except）"),
        ("control_error", vi._KIND_CONTROL, 0, control_meta_error.encode("utf-8"), b"",
         "控制：终止类错误广播（except 为 null）"),
        ("heartbeat", vi._KIND_HEARTBEAT, 0, b"", b"",
         "心跳：1s 无事件发一次，meta 与 jpeg 皆空"),
        ("ingest", vi._KIND_INGEST, 0, ingest_meta.encode("utf-8"), b"",
         "入库：meta 含 gen/seq 与待写行，jpeg 空；仅内网，不下发浏览器"),
    ]
    examples = []
    for name, kind, frame_id, meta, payload, note in cases:
        examples.append({
            "name": name, "kind": kind, "frame_id": frame_id,
            "meta_utf8": meta.decode("utf-8"),
            "jpeg_hex": payload.hex(),
            "envelope_hex": vi._envelope(kind, frame_id, meta, payload).hex(),
            "note": note,
        })

    return {
        "description": ("上行帧流 GET /internal/vision/events 的信封（HTTP/1.1 chunked，"
                        "单向；两端纯标准库，`curl -N` 可直接观察）"),
        "magic": vi._MAGIC.decode("ascii"),
        "version": vi._VERSION,
        "kinds": {"frame": vi._KIND_FRAME, "control": vi._KIND_CONTROL,
                  "heartbeat": vi._KIND_HEARTBEAT, "ingest": vi._KIND_INGEST},
        "layout": ("magic(4B) | ver(1B) | kind(1B) | frame_id(4B 大端) | "
                   "metaLen(4B 大端) | jpegLen(4B 大端) | meta(UTF-8 JSON) | jpeg(原始字节)"),
        "heartbeat_interval_seconds": vi._HEARTBEAT_SECONDS,
        "note": ("文本消息一律由 Python 组装（含中文与 JSON 键序），Go 只转发 —— "
                 "kind=1 时把 meta.msg 逐字作为 WS 文本下发、jpeg 用 [4B 大端 frame_id] "
                 "前缀后作为二进制下发；kind=2 时按 texts 顺序下发、framed_tail 只给"
                 "「已收到过帧」的观看者（判据在 clear 复位之前求值）、except 指定的"
                 "观看者跳过、clear 表示丢弃待发帧并复位帧号。"),
        "examples": examples,
    }


def export_ws_messages() -> dict:
    packet_examples = []
    for frame_id, jpeg_hex in ((1, "ffd8ffd9"), (617, "ffd8ffd9"), (100000, "ffd8ff")):
        packet_examples.append({
            "frame_id": frame_id,
            "jpeg_hex": jpeg_hex,
            "packet_hex": wsp.frame_packet(frame_id, bytes.fromhex(jpeg_hex)).hex(),
        })

    schedule = {str(fid): wsp.should_send_metadata(fid)
                for fid in (1, 2, 4, 5, 6, 10, 11, 100)}

    meta_examples = [
        {
            "result": {
                "frame_id": 617,
                "timestamp": 1729000000.25,
                "tracks": [{"track_id": 1, "center": [640, 360],
                            "is_staff": False, "anomaly_score": 0.12}],
                "events": [{"type": "zone_enter", "zone_id": "shelf_1",
                            "track_id": 1, "detail": "进入货架区"}],
                "active_suspicious": [{"track_id": 1, "score": 0.66}],
            },
            "mode": "retail",
            "note": "零售模式（无 faces 键）",
        },
        {
            "result": {
                "frame_id": 5,
                "timestamp": 1729000000.5,
                "tracks": [],
                "events": [],
                "active_suspicious": [],
                "faces": [{"emotion": "happy", "emotion_cn": "高兴", "conf": 0.91}],
            },
            "mode": "emotion",
            "note": "表情模式（带 faces 键）",
        },
    ]
    for ex in meta_examples:
        ex["expect"] = wsp.frame_metadata_message(ex["result"], ex["mode"])

    status_kwargs = [
        {"state": "running", "echo": {"kind": "file", "path": "/x.mp4"},
         "message": "视频源已开始输出画面", "extra": {"frame_id": 1}},
        {"state": "error", "echo": {}, "code": "open_timeout",
         "message": "打开超时：超过等待上限没有产出画面，已停止。"},
        {"state": "stopped", "echo": {}, "message": "视频已停止"},
        {"state": "finished", "echo": {"kind": "client"},
         "code": "finished", "message": "视频播放完毕"},
    ]
    status_examples = []
    for kwargs in status_kwargs:
        ex = {"kwargs": kwargs}
        ex["expect"] = wsp.source_status_message(
            kwargs["state"], kwargs.get("echo"), kwargs.get("code"),
            kwargs.get("message"), **kwargs.get("extra", {}))
        status_examples.append(ex)

    return {
        "schema": 1,
        "generated_by": "contracts/export_fixtures.py",
        "source": "ws_protocol.py（唯一协议定义处）",
        "frame_packet": {
            "description": ("[4B 大端 frame_id][JPEG 原始字节]。与迁移前 base64 往返的下发字节"
                            "逐字等价 —— 去往返只改内部存储形态，对客户端零感知。"),
            "header_bytes": wsp.FRAME_HEADER_BYTES,
            "byte_order": "big-endian",
            "examples": packet_examples,
        },
        "metadata_schedule": {
            "interval_setting": "config.settings.WS_METADATA_INTERVAL",
            "interval": WS_METADATA_INTERVAL,
            "rule": "frame_id == 1 or frame_id % interval == 0",
            "examples": schedule,
        },
        "frame_metadata_examples": meta_examples,
        "source_status_examples": status_examples,
        "upstream_envelope": export_upstream_envelopes(),
        "messages": {
            "client_camera_busy": wsp.CLIENT_CAMERA_BUSY_MESSAGE,
            # 数据面迁 Go 后由 Go **自行合成**的两条文本消息（其余文本一律由 Python 组装、
            # Go 逐字转发）—— 它们必须与 Python 的字节完全一致
            "ready": wsp.dumps_message({"event": "ready"}),
            "pong_int": wsp.dumps_message(wsp.pong_message(0)),
            "pong_float": wsp.dumps_message(wsp.pong_message(1729000000.25)),
            # "视频流已停止"：停止/终止时发给「已收到过帧」的观看者
            "stream_stopped_tail": wsp.dumps_message(
                {"type": "status", "status": "stopped", "message": "视频流已停止"}),
        },
    }


# ---------------- auth_policy.json ----------------

def measure_fastapi_error_shape() -> dict:
    """**实测** FastAPI 默认异常处理器的 404 渲染结果（不手抄、不推测）。

    用一次性最小应用驱动：`/internal/**` 的兜底 404 与任何未知路径的 404
    都走 FastAPI 默认处理器，形状只取决于框架版本。
    edge 侧对 `/internal/**` 的本地 404 必须与之逐字一致（公网请求不出网，
    两条路径上同一个探测请求得到同样的响应）。
    """
    from fastapi import FastAPI, HTTPException
    from starlette.testclient import TestClient

    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise HTTPException(status_code=404)

    with warnings.catch_warnings():
        # starlette 对 testclient 的传输后端有过渡期弃用提示；这里只借它量一次渲染结果
        warnings.simplefilter("ignore")
        client = TestClient(app)
    with client:
        resp = client.get("/boom")
        return {
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "body": resp.text,
        }


def export_auth_policy() -> dict:
    import api.security as sec

    return {
        "schema": 1,
        "generated_by": "contracts/export_fixtures.py",
        "source": "api/security.py（_PUBLIC_PATHS / needs_auth / 未登录响应的常量）",
        "note": ("鉴权策略是跨语言的对外契约：Go edge 会按这几条判断"
                 "\"这个请求明显没凭据吗\"，并按 unauthorized 逐字复刻 401。"
                 "edge 只可能**提前拒绝**，放行与否的权威判定永远在 Python"
                 "（每个请求到 Python 后仍会自行鉴权）。改动本夹具即改动对外契约。"),
        "public_paths": sorted(sec._PUBLIC_PATHS),
        "needs_auth_rules": [
            "path in public_paths → 免登录",
            "path.startswith('/api/') → 需登录",
            "path.startswith(('/docs','/redoc','/openapi.json')) → 需登录（AUTH_PUBLIC_DOCS=1 时免）",
        ],
        "cookie_name": sec.COOKIE_NAME,
        "bearer_prefix": sec.BEARER_PREFIX,
        "unauthorized": {
            "status": sec.UNAUTHORIZED_STATUS,
            "content_type": sec.UNAUTHORIZED_CONTENT_TYPE,
            "body": sec.UNAUTHORIZED_BODY,
        },
        "not_found": measure_fastapi_error_shape(),
    }


def main() -> None:
    _dump("guard_cases.json", export_guard_cases())
    _dump("permissions.json", export_permissions())
    _dump("ws_messages.json", export_ws_messages())
    _dump("auth_policy.json", export_auth_policy())


if __name__ == "__main__":
    main()
