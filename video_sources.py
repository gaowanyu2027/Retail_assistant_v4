"""视频输入源的**统一描述、校验与能力发现**（A 档改造）

## 为什么要有这个模块

修复前，"视频输入"是**三个各自为政的 WS 动作**：

| 动作 | 语义 | 参数名 | 前端调用点 |
|---|---|---|---|
| `start_webcam` | 服务端开**本机设备** | `camera_id` | 摄像头菜单 |
| `start_file` | 服务端开**文件 / RTSP** | `file_path` | 上传视频、配置里的摄像头 |
| `start_client_camera` | 客户端推帧 | 无 | 本地摄像头 |

于是踩出一串问题：把**配置里的字符串 id**（`cam_in_01`）当**设备号**发、
`parseInt('cam_in_01')||0` 一律去开 0 号设备、动作被静默丢弃、
容器里必然失败的设备型源没有任何明确提示。

本模块把入口收敛成**一个** `open_source` + **一套描述符** `source`，
并给前端一个 `GET /api/video/sources` 让它**按能力渲染菜单**（而不是硬编码按钮）。

## 客户端发什么（source 描述符）

    {"kind": "camera", "id": "cam_in_01"}      # 服务端配置的摄像头（source 可信）
    {"kind": "device", "index": 0}             # 本机设备号
    {"kind": "file",   "path": ".../x.mp4"}    # 必须落在白名单目录内
    {"kind": "upload", "id": "x.mp4"}          # 上传接口返回的**文件名**（服务端映射到 data/videos/）
    {"kind": "client"}                         # 浏览器采帧（客户端随后推 client_frame）

## 安全边界（刻意收紧，别当成遗漏）

1. **客户端不能直传 `rtsp://` URL**：那等于让服务端去连任意地址（SSRF 面，见台账 B1）。
   RTSP 只能先经 `POST /api/cameras` 注册（需 `system:manage`），再以 `kind=camera` 使用。
2. **`kind=file` 只接受白名单目录内的路径**（台账 B6：修复前 `start_file` 接受任意路径，
   能读容器内任意可解码文件并探测存在性）。`kind=upload` 更是只接受**文件名**，
   真实路径由服务端拼，客户端看不到服务器路径。
3. **路径判断一律用 `Path.resolve()` 后比较**，不做字符串前缀匹配（`..`、符号链接、大小写）。"""
from __future__ import annotations

import os
import re
from pathlib import Path

from config.settings import (
    DATA_DIR,
    PROJECT_ROOT,
    VIDEO_ALLOWED_DIRS,
    VIDEO_SOURCE_ALLOW_HTTP,
    VIDEO_SOURCE_ALLOW_PRIVATE,
    VIDEO_SOURCE_ALLOW_PUBLIC,
    VIDEO_SOURCE_ALLOWED_SCHEMES,
    VIDEO_UPLOAD_SUBDIR,
)

# ---- 统一的 kind 取值 ----
KIND_CAMERA = "camera"
KIND_DEVICE = "device"
KIND_FILE = "file"
KIND_UPLOAD = "upload"
KIND_CLIENT = "client"

# 各 kind 对应的**老动作**（内部转发用；对外仍是同一套描述符）
_LEGACY = {
    KIND_DEVICE: "start_webcam",
    KIND_FILE: "start_file",
    KIND_CAMERA: "start_file",
    KIND_UPLOAD: "start_file",
    KIND_CLIENT: "start_client_camera",
}

_VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv"}


class SourceError(Exception):
    """统一的源错误：`code` 供前端机器判断，`message` 给人看。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ==================== 路径与目录 ====================

def upload_dir() -> Path:
    """上传视频的落盘目录（与 `POST /api/videos` 保持一致）。"""
    return PROJECT_ROOT / "data" / VIDEO_UPLOAD_SUBDIR


def allowed_dirs() -> list[Path]:
    """客户端可用 `kind=file` 打开的目录白名单（已 resolve）。"""
    out: list[Path] = []
    for raw in VIDEO_ALLOWED_DIRS:
        p = Path(raw)
        out.append((p if p.is_absolute() else PROJECT_ROOT / p).resolve())
    return out


def _inside(path: Path, roots: list[Path]) -> bool:
    """`path` 是否落在任一白名单目录内（用 resolve 后的路径比较，防 `..` 与符号链接）。"""
    for root in roots:
        try:
            if path == root or path.is_relative_to(root):
                return True
        except Exception:
            continue
    return False


# ==================== 环境能力 ====================

def device_support() -> dict:
    """本机设备型源是否可用（容器里几乎必然不可用）。

    - Windows 宿主：有 DirectShow ✓
    - Linux：看内核有没有给出 `/dev/video*`（**容器 / WSL2 里通常没有**，
      因为 Windows 的摄像头硬件不会被直通给 WSL2 的 Linux 内核，容器就更看不到）
    """
    if os.name == "nt":
        return {"supported": True, "reason": "Windows 宿主，使用 DirectShow 打开本机设备"}
    import glob as _glob
    devs = sorted(_glob.glob("/dev/video*"))
    if devs:
        return {"supported": True, "reason": f"检测到设备节点: {', '.join(devs[:4])}"}
    return {
        "supported": False,
        "reason": "当前环境没有摄像头设备节点（/dev/video* 不存在）："
                  "容器看不到宿主摄像头，Windows 上的 Docker 也无法把摄像头直通进 WSL2",
    }


# ==================== 描述符归一化 ====================

def _camera_by_id(cam_id: str) -> dict | None:
    try:
        from agents.module_registry import get_registry
        cam = get_registry().get_camera(cam_id)
        return cam.to_dict() if cam else None
    except Exception as e:                       # 注册表不可用不该把请求打挂
        print(f"[VideoSource][WARN] 读取摄像头配置失败: {e}")
        return None


def _classify_camera_source(source: str) -> str:
    """配置里的 source 属于哪一类（服务端配置可信，但仍要判断可用性）。"""
    s = str(source or "").strip()
    if s.startswith("rtsp://") or s.startswith("rtsps://") or s.startswith("http://") or s.startswith("https://"):
        return "url"
    if not s or s == "webcam" or s.isdigit():
        return "device"
    return "file"


def normalize(source, camera_ids: list[str] | None = None) -> dict:
    """把统一描述符归一化成"内部动作 + 参数 + 回显"。

    返回 `{"kind","legacy_action","params","echo"}`；出错抛 `SourceError`
    （**任何**失败都必须带 code，不能再出现"点了没反应"）。
    """
    if isinstance(source, str):                   # 容忍简写：直接给 kind 字符串
        source = {"kind": source}
    if not isinstance(source, dict):
        raise SourceError("bad_request", "缺少 source 描述符")
    kind = str(source.get("kind") or "").strip().lower()

    # ---- 客户端直传 URL：明确拒绝（SSRF 面，见 B1）----
    if kind in ("rtsp", "url", "http", "https"):
        raise SourceError(
            "unsupported_kind",
            "不接受客户端直传的流地址（服务端会替你去连任意地址）。"
            "请先用 POST /api/cameras 注册摄像头，再用 kind=camera 打开。",
        )
    if kind not in _LEGACY:
        raise SourceError("unsupported_kind",
                          f"未知的视频源类型 {kind!r}；支持: {', '.join(_LEGACY)}")

    # ---- camera：服务端配置可信源 ----
    if kind == KIND_CAMERA:
        cam_id = str(source.get("id") or source.get("camera_id") or "").strip()
        if not cam_id:
            raise SourceError("bad_request", "kind=camera 需要 id")
        cam = _camera_by_id(cam_id)
        if not cam:
            known = camera_ids or []
            raise SourceError("camera_not_found",
                              f"摄像头 {cam_id!r} 未注册" + (f"；已注册: {', '.join(known)}" if known else ""))
        raw = str(cam.get("source") or "")
        cls = _classify_camera_source(raw)
        if cls == "url":
            # B1 收口：服务端配置的地址同样要过协议/主机校验（root 门槛 + 纵深防御）
            safe_url = guard_url(raw, client_supplied=False)
            return {"kind": kind, "legacy_action": "start_file",
                    "params": {"file_path": safe_url},
                    "echo": {"kind": kind, "id": cam_id, "name": cam.get("name"),
                             "via": "url", "source": mask_credentials(safe_url)}}
        if cls == "device":
            dev = device_support()
            if not dev["supported"]:
                raise SourceError("device_not_supported", f"摄像头 {cam_id} 使用本机设备：{dev['reason']}")
            idx = 0 if raw in ("", "webcam") else int(raw)
            return {"kind": KIND_DEVICE, "legacy_action": "start_webcam",
                    "params": {"camera_id": idx},
                    "echo": {"kind": KIND_DEVICE, "id": cam_id, "name": cam.get("name"), "index": idx}}
        # 文件型：相对路径按**项目根**解析（与 multi_stream 的约定一致）
        # 服务端配置可信，但仍拒绝 UNC 并要求文件存在（B1 的"借用服务端身份"面）
        full = guard_path(raw, client_supplied=False)
        return {"kind": kind, "legacy_action": "start_file",
                "params": {"file_path": str(full)},
                "echo": {"kind": kind, "id": cam_id, "name": cam.get("name"), "via": "file", "source": str(full)}}

    # ---- device：本机设备号 ----
    if kind == KIND_DEVICE:
        dev = device_support()
        if not dev["supported"]:
            raise SourceError("device_not_supported", dev["reason"])
        try:
            idx = int(source.get("index", source.get("camera_id", 0)) or 0)
        except Exception:
            raise SourceError("bad_request", "device.index 必须是整数")
        if idx < 0:
            raise SourceError("bad_request", "device.index 不能为负")
        return {"kind": kind, "legacy_action": _LEGACY[kind],
                "params": {"camera_id": idx}, "echo": {"kind": kind, "index": idx}}

    # ---- upload：只接受文件名，服务端映射到上传目录 ----
    if kind == KIND_UPLOAD:
        raw = str(source.get("id") or source.get("filename") or "").strip()
        if not raw:
            raise SourceError("bad_request", "kind=upload 需要 id（上传接口返回的文件名）")
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw.replace("\\", "/").split("/")[-1]).strip()
        if not name:
            raise SourceError("bad_request", "上传文件名无效")
        if Path(name).suffix.lower() not in _VIDEO_EXT:
            raise SourceError("bad_request", f"不支持的视频扩展名: {Path(name).suffix}")
        full = (upload_dir() / name).resolve()
        if not _inside(full, [upload_dir().resolve()]):
            raise SourceError("file_not_allowed", "上传文件路径越界")
        if not full.exists():
            raise SourceError("file_not_found", f"上传的视频不存在或已被清理: {name}")
        return {"kind": kind, "legacy_action": "start_file",
                "params": {"file_path": str(full)},
                "echo": {"kind": kind, "id": name}}

    # ---- file：白名单目录内的路径（台账 B6 的收口）----
    if kind == KIND_FILE:
        raw = str(source.get("path") or source.get("file_path") or "").strip()
        if not raw:
            raise SourceError("bad_request", "kind=file 需要 path")
        full = guard_path(raw, client_supplied=True)     # 统一走守卫（UNC/控制字符/白名单/存在性）
        return {"kind": kind, "legacy_action": _LEGACY[kind],
                "params": {"file_path": str(full)}, "echo": {"kind": kind, "path": str(full)}}

    # ---- client：浏览器采帧 ----
    return {"kind": KIND_CLIENT, "legacy_action": _LEGACY[KIND_CLIENT],
            "params": {}, "echo": {"kind": KIND_CLIENT}}


def echo_for_legacy(action: str, msg: dict) -> dict:
    """把**老动作**也映射成统一描述符（回执用），使新老入口共享同一套状态。"""
    if action == "start_webcam":
        return {"kind": KIND_DEVICE, "index": msg.get("camera_id", 0)}
    if action == "start_file":
        return {"kind": KIND_FILE, "path": msg.get("file_path", "")}
    if action == "start_client_camera":
        return {"kind": KIND_CLIENT}
    return {}


# ==================== 源安全校验（台账 B1 / B6 的收口）====================
#
# 两条路径，同一个洞：**用户给的字符串被服务端当成视频源直接打开**。
# - **B1**：`POST /api/cameras` 注册的 `source`（root 门槛，属纵深防御）
# - **B6**：WS `start_file` / `open_source` 的路径（**任意登录账号**，本次复现实证）
#
# 修复前的实测（platform 账号，非 root）：
#     start_file /app/mmpose/demo/resources/demo.mp4  → ★ 读到画面
#     start_file /app/data/../mmpose/.../demo.mp4     → ★ 读到画面（`..` 也没拦）
#     不存在的路径                                     → "无法打开视频源: <路径>"（存在性探针）
#
# 设计取舍（写清楚，别当成遗漏）：
# - **环回 / 链路本地（含云元数据 169.254.169.254）/ 组播 / 保留 / 未指定** —— **永久封禁**，
#   任何白名单都不放行（这些地址要么打自己、要么打云的凭据服务，没有正当视频源场景）；
# - **私网**（192.168/10/172.16…）—— 默认**允许**：门店摄像头本来就在内网，一刀切封掉等于把
#   正常用法打死（本项目 `config/cameras.yaml` 里就有一台 `rtsp://192.168.10.20`）；
# - **公网** —— 默认**拒绝**（`VIDEO_SOURCE_ALLOW_PUBLIC=1` 可开）：公网拉流的正当性低得多；
# - **客户端给的路径**必须落在白名单目录内；**服务端配置的路径**只做"存在 + 非 UNC"检查
#   —— root 本来就有更大权限，这里防的是"借用服务端身份"（SSRF/UNC），不是防 root 读文件。
#
# ⚠ 已知局限：DNS 在守卫处解析一次，真正 open 时 cv2 会**再解析一次** → 理论上存在
#   DNS 重绑定窗口。彻底解决要在 open 时用"已解析的 IP"，本次未做（成本/收益不划算）。

def mask_credentials(text: str) -> str:
    """日志/回执里脱敏 URL 中的凭据（`rtsp://user:pass@host` → `rtsp://***:***@host`）。

    修复前 `rtsp://admin:密码@192.168.1.64/...` 会**明文进日志与错误回执**。
    """
    s = str(text or "")
    return re.sub(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/@\s]*@", r"\1***:***@", s)


def _classify_ip(ip) -> str:
    """把 IP 分成 blocked / private / public（IPv4-mapped IPv6 先还原，否则可绕过）。"""
    import ipaddress
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
            or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)):
        return "blocked"
    return "private" if ip.is_private else "public"


def guard_url(url: str, *, client_supplied: bool = False) -> str:
    """校验流地址（rtsp/rtsps，可选 http(s)）。失败抛 `SourceError`。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    s = str(url or "").strip()
    if not s:
        raise SourceError("bad_request", "流地址为空")
    if len(s) > 512:
        raise SourceError("bad_request", "流地址过长（>512）")
    if any(ch in s for ch in "\r\n\t "):
        raise SourceError("bad_request", "流地址含空白或控制字符")
    if "\\" in s:
        raise SourceError("unc_not_allowed", "不接受含反斜杠的地址（UNC 路径会让服务端外带 NTLM 凭据）")

    parsed = urlparse(s)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise SourceError("bad_request", "流地址缺少协议头")
    if scheme in ("http", "https"):
        if not VIDEO_SOURCE_ALLOW_HTTP:
            raise SourceError("unsupported_scheme",
                              f"暂不接受 {scheme}:// 视频源（可用 VIDEO_SOURCE_ALLOW_HTTP=1 开启）")
    elif scheme not in VIDEO_SOURCE_ALLOWED_SCHEMES:
        raise SourceError("unsupported_scheme",
                          f"不支持的协议 {scheme!r}；允许: {', '.join(VIDEO_SOURCE_ALLOWED_SCHEMES)}")

    host = parsed.hostname
    if not host:
        raise SourceError("bad_request", f"流地址缺少主机名: {mask_credentials(s)}")

    # 主机名可能解析到多个地址：**每一个**都要过检查
    try:
        infos = socket.getaddrinfo(host, None)
        ips = sorted({info[4][0] for info in infos})
    except Exception as e:
        raise SourceError("dns_failed", f"主机名无法解析: {host}（{type(e).__name__}）")
    if not ips:
        raise SourceError("dns_failed", f"主机名没有解析到任何地址: {host}")

    for raw_ip in ips:
        cls = _classify_ip(ipaddress.ip_address(raw_ip))
        if cls == "blocked":
            raise SourceError(
                "host_blocked",
                f"目标 {host} 解析到被禁止的地址 {raw_ip}："
                f"环回/链路本地(含云元数据 169.254.169.254)/组播/保留地址一律不允许",
            )
        if cls == "private" and not VIDEO_SOURCE_ALLOW_PRIVATE:
            raise SourceError("host_blocked",
                              f"目标 {host} 解析到私网地址 {raw_ip}，当前配置不允许（VIDEO_SOURCE_ALLOW_PRIVATE=0）")
        if cls == "public" and not VIDEO_SOURCE_ALLOW_PUBLIC:
            raise SourceError("host_blocked",
                              f"目标 {host} 解析到公网地址 {raw_ip}，默认拒绝公网视频源"
                              f"（如确需请设 VIDEO_SOURCE_ALLOW_PUBLIC=1）")
    return s


def guard_path(path: str, *, client_supplied: bool) -> Path:
    """校验文件路径。失败抛 `SourceError`。

    `client_supplied=True` 时（= 来自客户端的路径，台账 B6）必须落在白名单目录内；
    `False`（= 服务端 config/cameras.yaml 里的路径，台账 B1）只做 UNC/存在性/扩展名检查。
    """
    raw = str(path or "").strip()
    if not raw:
        raise SourceError("bad_request", "文件路径为空")
    if len(raw) > 512:
        raise SourceError("bad_request", "文件路径过长（>512）")
    if any(ch in raw for ch in "\r\n\t\x00"):
        raise SourceError("bad_request", "文件路径含控制字符")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise SourceError("unc_not_allowed",
                          "不接受 UNC 路径（\\\\host\\share）：服务端会主动去认证并外带 NTLM 凭据")

    p = Path(raw)
    full = (p if p.is_absolute() else PROJECT_ROOT / p).resolve()
    if client_supplied:
        roots = allowed_dirs()
        if not _inside(full, roots):
            raise SourceError(
                "file_not_allowed",
                f"路径不在允许的目录内（这是刻意限制：修复前该接口能打开容器内**任意**文件）。"
                f"允许: {', '.join(str(r) for r in roots)}",
            )
    if not full.exists():
        raise SourceError("file_not_found", f"文件不存在: {full}")
    if full.suffix.lower() not in _VIDEO_EXT:
        raise SourceError("bad_request", f"不支持的视频扩展名: {full.suffix or '(无)'}")
    return full


def guard_source(source: str, *, client_supplied: bool = False) -> str:
    """统一入口：校验一个"摄像头 source"字符串，返回规范化后的值。

    设备型（`webcam` / 纯数字）没有路径与主机风险，直接放行。
    """
    s = str(source or "").strip()
    if not s or s.lower() == "webcam" or s.isdigit():
        return s
    low = s.lower()
    if low.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        return guard_url(s, client_supplied=client_supplied)
    return str(guard_path(s, client_supplied=client_supplied))



# ==================== 能力发现（供前端渲染菜单）====================

def capabilities() -> dict:
    """`GET /api/video/sources` 的返回体。

    `available` / `reason` 让前端**置灰并说明原因**，而不是让用户点了没反应。
    """
    dev = device_support()
    cameras = []
    try:
        from agents.module_registry import get_registry
        raw_cameras = get_registry().list_cameras()
    except Exception as e:
        print(f"[VideoSource][WARN] 摄像头列表读取失败: {e}")
        raw_cameras = []
    for cam in raw_cameras:
        src = str(cam.get("source") or "")
        cls = _classify_camera_source(src)
        item = {"id": cam.get("id"), "name": cam.get("name"), "kind": cls,
                "source": src, "available": True, "reason": None, "verified": True}
        if cls == "url":
            item.update(verified=False, reason="未验证：需要网络可达（容器里连不到就会报错）")
        elif cls == "device":
            item.update(available=dev["supported"], reason=None if dev["supported"] else dev["reason"])
        else:
            p = Path(src)
            full = (p if p.is_absolute() else PROJECT_ROOT / p)
            if not full.exists():
                item.update(available=False,
                            reason=f"文件在服务端不存在: {full}（未挂载/被 .dockerignore 排除）")
        cameras.append(item)

    return {
        "kinds": [
            {"kind": KIND_CAMERA, "available": True,
             "note": "打开服务端已注册的摄像头（source 由服务端配置，可信）",
             "count": len(cameras)},
            {"kind": KIND_DEVICE, "available": dev["supported"], "reason": dev["reason"],
             "note": "打开服务端所在机器的本机摄像头设备"},
            {"kind": KIND_FILE, "available": True,
             "note": "打开服务端白名单目录内的视频文件",
             "allowed_dirs": [str(d) for d in allowed_dirs()]},
            {"kind": KIND_UPLOAD, "available": True,
             "note": "打开已上传的视频（POST /api/videos 返回 filename 后用 kind=upload）",
             "upload_dir": str(upload_dir())},
            {"kind": KIND_CLIENT, "available": True,
             "note": "浏览器采帧（getUserMedia）：容器里也能用，摄像头归浏览器所在机器"},
        ],
        "cameras": cameras,
        "limits": {
            "video_ext": sorted(_VIDEO_EXT),
            "max_upload_mb": None,          # 上传大小限制见台账 B11（尚未实现）
        },
    }
